#!/usr/bin/env python
# -*- coding: utf-8 -*-
# vim: tabstop=4 expandtab shiftwidth=4 softtabstop=4
# Author: "Chris Ward" <cward@redhat.com>

'''
metrique.cubes.sqldata.generic
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

This module contains the cube methods for extracting
data from generic SQL data sources.
'''

from __future__ import unicode_literals

import logging
logger = logging.getLogger(__name__)

from copy import deepcopy
from collections import defaultdict
from dateutil.parser import parse as dt_parse
from functools import partial
import os
import pandas as pd
import pytz
import re

try:
    from joblib import Parallel, delayed
    HAS_JOBLIB = True
except ImportError:
    HAS_JOBLIB = False
    logger.warn("joblib package not found!")

import warnings

from metrique import pyclient
from metrique.utils import batch_gen, configure


def get_full_history(cube, oids, flush=False, cube_name=None, autosnap=False,
                     config=None, **kwargs):
    m = pyclient(cube=cube, name=cube_name, config=config, **kwargs)
    results = []
    batch_size = m.config[m.config_key].get('batch_size')
    for batch in batch_gen(oids, batch_size):
        _ = m._activity_get_objects(oids=batch, flush=flush, autosnap=autosnap)
        results.extend(_)
    return results


def get_objects(cube, oids, flush=False, cube_name=None, autosnap=True,
                config=None, **kwargs):
    m = pyclient(cube=cube, name=cube_name, config=config, **kwargs)
    results = []
    batch_size = m.config[m.config_key].get('batch_size')
    for batch in batch_gen(oids, batch_size):
        _ = m._get_objects(oids=batch, flush=flush, autosnap=autosnap)
        results.extend(_)
    return results


class Generic(pyclient):
    '''
    Base, common functionality driver for connecting
    and extracting data from SQL databases.

    It is expected that specific database connectors will
    subclass this basecube to implement db specific connection
    methods, etc.

    :cvar fields: cube fields definitions
    :cvar defaults: cube default property container (cube specific meta-data)

    :param sql_host: teiid hostname
    :param sql_port: teiid port
    :param sql_username: sql username
    :param sql_password: sql password
    :param sql_retries: number of times before we give up on a query
    :param sql_batch_size: how many objects to query at a time
    '''
    dialect = None
    config_key = 'sqlalchemy'
    fields = None

    def __init__(self, vdb=None, retries=None, batch_size=None,
                 worker_batch_size=None,
                 config_key=None, config_file=None,
                 **kwargs):
        super(Generic, self).__init__(**kwargs)
        # FIXME: alias == self.schema
        self.fields = self.fields or {}
        options = dict(vdb=vdb,
                       retries=retries,
                       batch_size=batch_size,
                       worker_batch_size=worker_batch_size
                       )
        defaults = dict(vdb=None,
                        retries=1,
                        batch_size=1000,
                        worker_batch_size=5000,
                        )
        self.config = self.config or {}
        config_key = config_key or self.config_key
        self.config = configure(options, defaults,
                                config_file=config_file,
                                section_key=config_key,
                                update=self.config)
        self.retry_on_error = (Exception, )
        self._setup_inconsistency_log()

    def activity_get(self, ids=None):
        '''
        Returns a dictionary of `id: [(when, field, removed, added)]`
        key:value pairs that represent the activity history for
        the particular ids.

        This is used when originating data source has some form of
        a 'change log' table that tracks changes to individual
        object.fields.

        The data returned by this method is used to rebuild historical
        object states.
        '''
        raise NotImplementedError(
            'The activity_get method is not implemented in this cube.')

    def _activity_get_objects(self, oids, flush=False, autosnap=False):
        logger.debug('Getting Objects - Activity History')
        self._get_objects(oids=oids, flush=False)
        objects = self.objects.values()
        # dict, has format: oid: [(when, field, removed, added)]
        activities = self.activity_get(oids)
        for doc in objects:
            _oid = doc['_oid']
            acts = activities.setdefault(_oid, [])
            objs = self._activity_import_doc(doc, acts)
            self.objects.extend(objs)
        if flush:
            return self.objects.flush(autosnap=autosnap, schema=self.fields)
        else:
            return self.objects.values()

    def _activity_import_doc(self, time_doc, activities):
        '''
        Import activities for a single document into timeline.
        '''
        batch_updates = [time_doc]
        # We want to consider only activities that happend before time_doc
        # do not move this, because time_doc._start changes
        # time_doc['_start'] is a timestamp, whereas act[0] is a datetime
        td_start = time_doc['_start']
        activities = filter(lambda act: (act[0] < td_start and
                                         act[1] in time_doc), activities)
        config = self.config[self.config_key]
        creation_field = config.get('cfield')
        # make sure that activities are sorted by when descending
        activities.sort(reverse=True, key=lambda o: o[0])
        new_doc = {}
        for when, field, removed, added in activities:
            last_doc = batch_updates.pop()
            # check if this activity happened at the same time as the last one,
            # if it did then we need to group them together
            if last_doc['_end'] == when:
                new_doc = deepcopy(last_doc)
                last_doc = batch_updates.pop()
            else:
                new_doc = deepcopy(last_doc)
                new_doc['_start'] = when
                new_doc['_end'] = when
                last_doc['_start'] = when
            last_val = last_doc[field]

            # FIXME: pass in field and call _type() within _activity_backwards?
            # for added/removed?
            new_val, inconsistent = self._activity_backwards(new_doc[field],
                                                             removed, added)
            new_doc[field] = new_val

            # Check if the object has the correct field value.
            if inconsistent:
                self._log_inconsistency(last_doc, last_val, field,
                                        removed, added, when)
                new_doc.setdefault('_corrupted', {})
                # set curreupted field value to the the value that was added
                # and continue processing as if that issue didn't exist
                new_doc['_corrupted'][field] = added
            # Add the objects to the batch
            batch_updates.extend([last_doc, new_doc])
        # try to set the _start of the first version to the creation time
        try:
            # set start to creation time if available
            last_doc = batch_updates[-1]
            if creation_field:
                creation_ts = last_doc[creation_field]
                if creation_ts < last_doc['_start']:
                    last_doc['_start'] = creation_ts
                elif len(batch_updates) == 1:
                    # we have only one version, that we did not change
                    return []
                else:
                    pass  # leave as-is
        except Exception as e:
            logger.error('Error updating creation time; %s' % e)
        return batch_updates

    def _activity_backwards(self, val, removed, added):
        if isinstance(added, list) and isinstance(removed, list):
            val = [] if val is None else val
            inconsistent = False
            for ad in added:
                if ad in val:
                    val.remove(ad)
                else:
                    inconsistent = True
            val.extend(removed)
        else:
            inconsistent = val != added
            val = removed
        return val, inconsistent

    def _convert(self, field, value):
        convert = self.fields[field].get('convert')
        container = self.fields[field].get('container')
        if value is None:
            return None
        elif convert and container:
            # FIXME: callers need to make convert STATIC (no self)
            _convert = partial(convert)
            value = map(_convert, value)
        elif convert:
            value = convert(value)
        else:
            value = value
        return value

    def _delta_force(self, force=None, last_update=None, parse_timestamp=None):
        config = self.config[self.config_key]
        force = force or config.get('force') or False
        oids = []
        if force is True:
            # get a list of all known object ids
            oids = self.sql_get_oids()
        elif not force:
            if config.get('delta_new_ids', True):
                # get all new (unknown) oids
                oids.extend(self.get_new_oids())
            if config.get('delta_mtime', False):
                # get only those oids that have changed since last update
                oids.extend(self.get_changed_oids(last_update,
                                                  parse_timestamp))
        elif isinstance(force, (list, tuple, set)):
            oids = list(force)
        else:
            force = [force]
        logger.debug("Delta Size: %s" % len(oids))
        return sorted(set(oids))

    def get_changed_oids(self, last_update=None, parse_timestamp=None):
        '''
        Returns a list of object ids of those objects that have changed since
        `mtime`. This method expects that the changed objects can be
        determined based on the `delta_mtime` property of the cube which
        specifies the field name that carries the time of the last change.

        This method is expected to be overriden in the cube if it is not
        possible to use a single field to determine the time of the change and
        if another approach of determining the oids is available. In such
        cubes the `delta_mtime` property is expected to be set to `True`.

        If `delta_mtime` evaluates to False then this method is not expected
        to be used.

        :param mtime: datetime string used as 'change since date'
        '''
        mtime = self._fetch_mtime(last_update, parse_timestamp)
        config = self.config[self.config_key]
        mtime_columns = config.get('delta_mtime', [])
        if not (mtime_columns and mtime):
            return []
        if isinstance(mtime_columns, basestring):
            mtime_columns = [mtime_columns]
        where = []
        for _column in mtime_columns:
            _sql = "%s > %s" % (_column, mtime)
            where.append(_sql)
        return self.sql_get_oids(where)

    def _fetch_mtime(self, last_update=None, parse_timestamp=None):
        mtime = None
        if last_update:
            if isinstance(last_update, basestring):
                mtime = dt_parse(last_update)
            else:
                mtime = last_update
        else:
            mtime = self.get_last_field('_start')

        logger.debug("Last update mtime: %s" % mtime)

        config = self.config[self.config_key]
        if mtime:
            if parse_timestamp is None:
                parse_timestamp = config.get('parse_timestamp', True)
            if parse_timestamp:
                if not (hasattr(mtime, 'tzinfo') and mtime.tzinfo):
                    # We need the timezone, to readjust relative to the
                    # server's tz
                    mtime = mtime.replace(tzinfo=pytz.utc)
                mtime = mtime.strftime('%Y-%m-%d %H:%M:%S %z')
                dt_format = "yyyy-MM-dd HH:mm:ss z"
                mtime = "parseTimestamp('%s', '%s')" % (mtime, dt_format)
            else:
                mtime = "'%s'" % mtime
        return mtime

    @property
    def fieldmap(self):
        '''
        Dictionary of field_id: field_name, as defined in self.fields property
        '''
        if hasattr(self, '_sql_fieldmap') and self._sql_fieldmap:
            fieldmap = self._sql_fieldmap
        else:
            fieldmap = defaultdict(str)
            for field, opts in self.fields.iteritems():
                field_id = opts.get('what')
                if field_id is not None:
                    fieldmap[field_id] = field
            self._sql_fieldmap = fieldmap
        return fieldmap

    def _generate_sql(self, _oids=None, sort=True):
        config = self.config[self.config_key]
        db = config.get('db')
        _oid = config.get('_oid')
        table = config.get('table')

        if not all((_oid, table, db)):
            raise ValueError("Must define db, table, _oid in config!")
        selects = []
        stmts = []
        for as_field, opts in self.fields.iteritems():
            select = opts.get('select') or '%s.%s' % (table, as_field)
            select = '%s as %s' % (select, as_field)
            selects.append(select)
            sql = opts.get('sql') or ''
            sql = re.sub('\s+', ' ', sql)
            if sql:
                stmts.append(sql)

        selects = ', '.join(selects)
        stmts = ' '.join(stmts)
        sql = 'SELECT %s FROM %s.%s %s' % (selects, db, table, stmts)
        if _oids:
            sql += ' WHERE %s.%s in (%s)' % (table, _oid,
                                             ','.join(map(str, _oids)))
        if sort:
            sql += " ORDER BY %s.%s ASC" % (table, _oid)
        sql = re.sub('\s+', ' ', sql)
        return sql

    def get_objects(self, force=None, last_update=None, parse_timestamp=None,
                    flush=False, autosnap=True):
        '''
        Extract routine for SQL based cubes.

        :param force:
            for querying for all objects (True) or only those passed in as list
        :param last_update: manual override for 'changed since date'
        :param parse_timestamp: flag to convert timestamp timezones in-line
        '''
        workers = self.config['metrique'].get('workers')
        # if we're using multiple workers, break the oids
        # according to worker batchsize, then each worker will
        # break the batch into smaller sql batch size batches
        # otherwise, single threaded, use sql batch size
        config = self.config[self.config_key]
        w_batch_size = config.get('worker_batch_size')
        s_batch_size = config.get('batch_size')
        # set the 'index' of sql columns so we can extract
        # out the sql rows and know which column : field
        # determine which oids will we query
        oids = self._delta_force(force, last_update, parse_timestamp)

        if HAS_JOBLIB and workers > 1:
            logger.debug(
                'Getting Objects - Current Values (%s@%s)' % (
                    workers, w_batch_size))
            runner = Parallel(n_jobs=workers)
            func = delayed(get_objects)
            with warnings.catch_warnings():
                # suppress warning from joblib:
                # UserWarning: Parallel loops cannot be nested ...
                warnings.simplefilter("ignore")
                result = runner(func(
                    cube=self._cube, oids=batch, flush=flush,
                    cube_name=self.name, autosnap=autosnap,
                    config=self.config)
                    for batch in batch_gen(oids, w_batch_size))
            # merge list of lists (batched) into single list
            result = [i for l in result for i in l]
        else:
            logger.debug(
                'Getting Objects - Current Values (%s@%s)' % (
                    workers, s_batch_size))
            result = []
            for batch in batch_gen(oids, s_batch_size):
                _ = self._get_objects(oids=batch, flush=flush,
                                      autosnap=autosnap)
                result.extend(_)
        if flush:
            return result
        else:
            [self.objects.add(obj) for obj in result]
            return self

    def _get_objects(self, oids, flush=False, autosnap=True):
        config = self.config[self.config_key]
        retries = config.get('retries') or 1
        _oid = config.get('_oid')
        sql = self._generate_sql(oids)
        while retries > 0:
            try:
                objects = self._load_sql(sql)
                break
            except self.retry_on_error as e:
                logger.error('Fetch Failed: %s' % e)
                if retries <= 1:
                    raise
                else:
                    retries -= 1
        else:
            raise RuntimeError(
                "Failed to fetch any objects from %s!" % len(oids))
        # set _oid
        self.objects = self._prep_objects(objects)
        [o.update({'_oid': o[_oid]}) for o in objects]
        self.objects = objects
        if flush:
            return self.objects.flush(autosnap=autosnap, schema=self.fields)
        else:
            return self.objects.values()

    def get_new_oids(self):
        '''
        Returns a list of unique oids that have not been extracted yet.

        Essentially, a diff of distinct oids in the source database
        compared to cube.
        '''
        config = self.config[self.config_key]
        table = config.get('table')
        _oid = config.get('_oid')
        last_id = self.get_last_field('_oid')
        ids = []
        if last_id:
            try:  # try to convert to integer... if not, assume unicode value
                last_id = float(last_id)
                where = "%s.%s > %s" % (table, _oid, last_id)
            except (TypeError, ValueError):
                where = "%s.%s > '%s'" % (table, _oid, last_id)
            ids = self.sql_get_oids(where)
        return ids

    def get_full_history(self, force=None, last_update=None,
                         parse_timestamp=None, flush=False, autosnap=False):
        '''
        Fields change depending on when you run activity_import,
        such as "last_updated" type fields which don't have activity
        being tracked, which means we'll always end up with different
        hash values, so we need to always remove all existing object
        states and import fresh
        '''
        workers = self.config[self.global_config_key].get('workers')
        config = self.config[self.config_key]
        w_batch_size = config.get('worker_batch_size')
        s_batch_size = config.get('batch_size')
        # determine which oids will we query
        oids = self._delta_force(force, last_update, parse_timestamp)
        if HAS_JOBLIB and workers > 1:
            logger.debug(
                'Getting Full History (%s@%s)' % (
                    workers, w_batch_size))
            runner = Parallel(n_jobs=workers)
            func = delayed(get_full_history)
            with warnings.catch_warnings():
                # suppress warning from joblib:
                # UserWarning: Parallel loops cannot be nested ...
                warnings.simplefilter("ignore")
                result = runner(func(
                    cube=self._cube, oids=batch, flush=flush,
                    cube_name=self.name, autosnap=autosnap,
                    config=self.config)
                    for batch in batch_gen(oids, w_batch_size))
            # merge list of lists (batched) into single list
            result = [i for l in result for i in l]
        else:
            logger.debug(
                'Getting Full History (%s@%s)' % (
                    workers, s_batch_size))
            result = []
            for batch in batch_gen(oids, s_batch_size):
                _ = self._activity_get_objects(oids=batch, flush=flush,
                                               autosnap=autosnap)
                result.extend(_)

        if flush:
            return result
        else:
            [self.objects.add(obj) for obj in result]
            return self

    def _left_join(self, select_as, select_prop, join_prop, join_table,
                   on_col, on_db=None, on_table=None, join_db=None):
        config = self.config[self.config_key]
        on_table = on_table or config.get('table')
        on_db = on_db or config.get('db')
        join_db = join_db or config.get('db')
        return {'select': '%s.%s' % (select_as, select_prop),
                'sql': 'LEFT JOIN %s.%s %s ON %s.%s = %s.%s.%s' % (
                    join_db, join_table, select_as, select_as, join_prop,
                    on_db, on_table, on_col)}

    # FIXME: as_dict -> raw? we're returning a list... not dict
    def _load_sql(self, sql, dialect=None, as_dict=True,
                  cached=True, **kwargs):
        dialect = dialect or self.dialect
        # load sql kwargs from instance config
        _kwargs = deepcopy(self.config[self.config_key])
        # override anything passed in
        _kwargs.update(kwargs)
        engine = self.proxy.get_engine(cached=cached, **_kwargs)
        rows = engine.execute(sql)
        objects = [dict(row) for row in rows]
        if not as_dict:
            objects = pd.DataFrame(objects)
        return objects

    def _log_inconsistency(self, last_doc, last_val, field, removed, added,
                           when):
        incon = {'oid': last_doc['_oid'],
                 'field': field,
                 'removed': removed,
                 'removed_type': str(type(removed)),
                 'added': added,
                 'added_type': str(type(added)),
                 'last_val': last_val,
                 'last_val_type': str(type(last_val)),
                 'when': str(when)}
        m = u'{oid} {field}: {removed}-> {added} has {last_val}; '
        m += u'({removed_type}-> {added_type} has {last_val_type})'
        m += u' ... on {when}'
        self.log_inconsistency(m.format(**incon))

    def _normalize_container(self, field, value):
        container = self.fields[field].get('container')
        is_list = isinstance(value, (list, tuple))
        if container and not is_list:
            # and normalize to be a singleton list
            # FIXME: SHOULD WE NORMALIZE to empty list []?
            return [value] if value else None
        elif not container and is_list:
            raise ValueError(
                "Expected single value (%s), got list (%s)" % (
                    field, value))
        else:
            return value

    def _prep_objects(self, objects):
        for o in objects:
            for field, value in o.iteritems():
                value = self._unwrap(field, value)
                value = self._normalize_container(field, value)
                value = self._convert(field, value)
                value = self._typecast(field, value)
                o[field] = value

    @property
    def proxy(self):
        print 'Z'*10
        if not hasattr(self, '_sqldata_proxy'):
            #url = 'dialect+driver://username:password@host:port/database'
            dialect = self.dialect
            config = self.config[self.config_key]
            username = config.get('username')
            password = config.get('password')
            host = config.get('host')
            port = config.get('port')
            vdb = config.get('vdb')
            engine = '%s://%s:%s@%s:%s/%s' % (
                dialect, username, password, host, port, vdb
            )
            self._sqldata_proxy = self.sqla(engine=engine, **config)
        return self._sqldata_proxy

    def _typecast(self, field, value):
        _type = self.fields[field].get('type')
        if self.fields[field].get('container'):
            value = self._type_container(value, _type)
        else:
            value = self._type_single(value, _type)
        return value

    def _type_container(self, value, _type):
        ' apply type to all values in the list '
        if value is None:
            # normalize null containers to empty list
            return []
        elif not isinstance(value, (list, tuple)):
            raise ValueError("expected list type, got: %s" % type(value))
        else:
            return sorted(self._type_single(item, _type) for item in value)

    def _type_single(self, value, _type):
        ' apply type to the single value '
        if value is None:  # don't convert null values
            pass
        elif value == '':
            value = None
        elif _type is None:
            if isinstance(value, unicode):
                value = value.encode('utf8')
            else:
                value = unicode(str(value), 'utf8')
        elif isinstance(value, _type):  # or values already of correct type
            pass
        else:
            value = _type(value)
            if isinstance(value, unicode):
                value = value.encode('utf8')
            elif isinstance(value, str):
                value = unicode(value, 'utf8')
            else:
                pass  # leave as-is
        return value

    def _unwrap(self, field, value):
        if type(value) is buffer:
            # unwrap/convert the aggregated string 'buffer'
            # objects to string
            value = unicode(str(value), 'utf8')
            # FIXME: this might cause issues if the buffered
            # text has " quotes...
            value = value.replace('"', '').strip()
            if not value:
                value = None
            else:
                value = value.split('\n')
        return value

    def _setup_inconsistency_log(self):
        gconfig = self.config[self.global_config_key]
        _log_file = gconfig.get('log_file').split('.log')[0]
        basename = _log_file + '.inconsistencies'
        log_file = basename + '.log'
        log_dir = gconfig.get('log_dir')
        log_file = os.path.join(log_dir, log_file)

        logger_name = 'incon'
        logger = logging.getLogger(logger_name)
        hdlr = logging.FileHandler(log_file)
        log_format = logging.Formatter("%(message)s")
        hdlr.setFormatter(log_format)
        logger.addHandler(hdlr)
        logger.setLevel(logging.ERROR)
        logger.propagate = 0
        self.log_inconsistency = logger.error

    def sql_get_oids(self, where=None):
        '''
        Query source database for a distinct list of oids.
        '''
        config = self.config[self.config_key]
        table = config.get('table')
        db = config.get('db')
        _oid = config.get('_oid')
        sql = 'SELECT DISTINCT %s.%s FROM %s.%s' % (table, _oid, db, table)
        if where:
            where = [where] if isinstance(where, basestring) else list(where)
            sql += ' WHERE %s' % ' OR '.join(where)
        return sorted([r[_oid] for r in self._load_sql(sql)])
