#!/usr/bin/env python
# -*- coding: utf-8 -*-
# vim: tabstop=4 expandtab shiftwidth=4 softtabstop=4
# Author: "Chris Ward" <cward@redhat.com>

'''
metrique.utils
~~~~~~~~~~~~~~~~~

This module contains utility functions shared between
metrique sub-modules
'''

from __future__ import unicode_literals

import logging
logger = logging.getLogger(__name__)

import anyconfig
anyconfig.set_loglevel(logging.WARN)  # too noisy...
from calendar import timegm
import collections
from datetime import datetime
from dateutil.parser import parse as dt_parse
from hashlib import sha1
import locale
import os
import pytz
import re
import simplejson as json
import sys

json_encoder = json.JSONEncoder()

DEFAULT_PKGS = ['metrique.cubes']

SHA1_HEXDIGEST = lambda o: sha1(repr(o)).hexdigest()
UTC = pytz.utc


def configure(options, defaults, config_file=None,
              section_key=None, update=None, force=False,
              section_only=False):
    config = update or {}
    # FIXME: permit list of section keys to lookup values in
    sk = section_key
    sk = sk or options.get('config_key') or defaults.get('config_key')
    if not sk:
        sk = 'global'
        section_only = True
    elif sk in config and not force:
        # if 'sql' is already configured, ie, we initiated with
        # config set already, don't set defaults, only options
        # not set as None
        config.setdefault(sk, {})
        [config[sk].update({k: v})
         for k, v in options.iteritems() if v is not None]
    else:
        # load the config options from disk, if path provided
        section = {}
        if config_file:
            raw_config = load_config(config_file)
            section = raw_config.get(sk, {})
            if not isinstance(section, dict):
                # convert mergeabledict (anyconfig) to dict of dicts
                section = section.convert_to(section)
            defaults = rupdate(defaults, section)
        # set option to value passed in, if any
        for k, v in options.iteritems():
            v = v if v is not None else defaults[k]
            section[unicode(k)] = v
        config.setdefault(sk, {})
        config[sk] = rupdate(config[sk], section)
    if section_only:
        return config.get(sk)
    else:
        return config


def batch_gen(data, batch_size):
    '''
    Usage::
        for batch in batch_gen(iter, 100):
            do_something(batch)
    '''
    data = data or []
    for i in range(0, len(data), batch_size):
        yield data[i:i + batch_size]


def clear_stale_pids(pids, pid_dir, prefix=''):
    'check for and remove any pids which have no corresponding process'
    procs = os.listdir('/proc')
    running = [pid for pid in pids if pid in procs]
    _running = []
    prefix = '%s.' % prefix if prefix else ''
    for pid in pids:
        if pid in running:
            _running.append(pid)
        else:
            pid_file = '%s%s.pid' % (prefix, pid)
            path = os.path.join(pid_dir, pid_file)
            if os.path.exists(path):
                try:
                    os.remove(path)
                except OSError as e:
                    logger.debug(e)
    return _running


def csv2list(item):
    if isinstance(item, basestring):
        items = item.split(',')
    else:
        raise TypeError('Expected a csv string')
    items = [s.strip() for s in items]
    return items


def cube_pkg_mod_cls(cube):
    '''
    Used to dynamically importing cube classes
    based on string slug name.

    Converts 'pkg_mod' -> pkg, mod, Cls

    eg: tw_tweet -> tw, tweet, Tweet

    Assumes `Metrique Cube Naming Convention` is used

    :param cube: cube name to use when searching for cube pkg.mod.class to load
    '''
    _cube = cube.split('_')
    pkg = _cube[0]
    mod = '_'.join(_cube[1:])
    _cls = ''.join([s[0].upper() + s[1:] for s in _cube[1:]])
    return pkg, mod, _cls


def dt2ts(dt, drop_micro=False, strict=False):
    ''' convert datetime objects to timestamp seconds (float) '''
    # the equals check to 'NaT' is hack to avoid adding pandas as a dependency
    if dt != dt or repr(dt) == 'NaT' or not dt:
        msg = "invalid datetime '%s'" % dt
        if strict:
            raise ValueError(msg)
        else:
            logger.debug(msg)
            return None
    elif isinstance(dt, (int, long, float)):  # its a ts already
        ts = dt
    elif isinstance(dt, basestring):  # convert to datetime first
        ts = dt2ts(dt_parse(dt))
    else:
        # FIXME: microseconds/milliseconds are being dropped!
        # see: http://stackoverflow.com/questions/7031031
        # for possible solution?
        ts = timegm(dt.timetuple())
    if drop_micro:
        return float(int(ts))
    else:
        return float(ts)


def _load_cube_pkg(pkg, cube):
    '''
    NOTE: all items in fromlist must be strings
    '''
    try:
        # First, assume the cube module is available
        # with the name exactly as written
        fromlist = map(str, [cube])
        mcubes = __import__(pkg, fromlist=fromlist)
        return getattr(mcubes, cube)
    except AttributeError:
        # if that fails, try to guess the cube module
        # based on cube 'standard naming convention'
        # ie, group_cube -> from group.cube import CubeClass
        _pkg, _mod, _cls = cube_pkg_mod_cls(cube)
        fromlist = map(str, [_cls])
        mcubes = __import__('%s.%s.%s' % (pkg, _pkg, _mod),
                            fromlist=fromlist)
        return getattr(mcubes, _cls)


def load_config(path):
    if not path:
        return {}
    else:
        config_file = os.path.expanduser(path)
        return anyconfig.load(config_file)


def get_cube(cube, init=False, pkgs=None, cube_paths=None, config=None,
             backends=None, **kwargs):
    '''
    Dynamically locate and load a metrique cube

    :param cube: name of the cube class to import from given module
    :param init: flag to request initialized instance or uninitialized class
    :param config: config dict to pass on initialization (implies init=True)
    :param pkgs: list of package names to search for the cubes in
    :param cube_path: additional paths to search for modules in (sys.path)
    :param kwargs: additional kwargs to pass to cube during initialization
    '''
    pkgs = pkgs or ['cubes']
    pkgs = [pkgs] if isinstance(pkgs, basestring) else pkgs
    # search in the given path too, if provided
    cube_paths = cube_paths or []
    cube_paths_is_basestring = isinstance(cube_paths, basestring)
    cube_paths = [cube_paths] if cube_paths_is_basestring else cube_paths
    cube_paths = [os.path.expanduser(path) for path in cube_paths]

    # append paths which don't already exist in sys.path to sys.path
    [sys.path.append(path) for path in cube_paths if path not in sys.path]

    pkgs = pkgs + DEFAULT_PKGS
    err = False
    for pkg in pkgs:
        try:
            _cube = _load_cube_pkg(pkg, cube)
        except ImportError as err:
            _cube = None
        if _cube:
            break
    else:
        logger.error(err)
        raise RuntimeError('"%s" not found! %s; %s \n%s)' % (
            cube, pkgs, cube_paths, sys.path))

    if init:
        _cube = _cube(config=config, **kwargs)
    return _cube


def get_pids(pid_dir, prefix='', clear_stale=True):
    pid_dir = os.path.expanduser(pid_dir)
    # eg, server.22325.pid, server.23526.pid
    pids = []
    prefix = '%s.' % prefix if prefix else ''
    for f in os.listdir(pid_dir):
        pid_re = re.search(r'%s(\d+).pid' % prefix, f)
        if pid_re:
            pids.append(pid_re.groups()[0])
    if clear_stale:
        pids = clear_stale_pids(pids, pid_dir, prefix)
    return map(int, pids)


def get_timezone_converter(from_timezone, tz_aware=False):
    '''
    return a function that converts a given
    datetime object from a timezone to utc

    :param from_timezone: timezone name as string
    '''
    from_tz = pytz.timezone(from_timezone)

    def timezone_converter(dt):
        if dt is None:
            return None
        elif isinstance(dt, basestring):
            dt = dt_parse(dt)
        if dt.tzinfo:
            # datetime instance already has tzinfo set
            # WARN if not dt.tzinfo == from_tz?
            try:
                dt = dt.astimezone(UTC)
            except ValueError:
                # date has invalid timezone; replace with expected
                dt = dt.replace(tzinfo=from_tz)
                dt = dt.astimezone(UTC)
        else:
            # set tzinfo as from_tz then convert to utc
            dt = from_tz.localize(dt).astimezone(UTC)
        if not tz_aware:
            dt = dt.replace(tzinfo=None)
        return dt
    return timezone_converter


def json_encode(obj):
    '''
    Convert datetime.datetime to timestamp

    :param obj: value to (possibly) convert
    '''
    if isinstance(obj, datetime):
        return dt2ts(obj)
    else:
        return json_encoder.default(obj)


def jsonhash(obj, root=True, exclude=None, hash_func=None):
    '''
    calculate the objects hash based on all field values
    '''
    if not hash_func:
        hash_func = SHA1_HEXDIGEST
    if isinstance(obj, dict):
        obj = obj.copy()  # don't affect the ref'd obj passed in
        keys = set(obj.iterkeys())
        if root and exclude:
            [obj.__delitem__(f) for f in exclude if f in keys]
        # frozenset's don't guarantee order; use sorted tuples
        # which means different python interpreters can return
        # back frozensets with different hash values even when
        # the content of the object is exactly the same
        result = sorted(
            (k, jsonhash(v, False)) for k, v in obj.items())
    elif isinstance(obj, list):
        # FIXME: should obj be sorted for consistent hashes?
        # when the object is the same, just different list order?
        result = tuple(jsonhash(e, False) for e in obj)
    else:
        result = obj
    if root:
        result = hash_func(repr(result))
    return result


def to_encoding(ustring, encoding=None):
    encoding = encoding or locale.getpreferredencoding()
    if isinstance(ustring, basestring):
        if not isinstance(ustring, unicode):
            return unicode(ustring, encoding, 'replace')
        else:
            return ustring.encode(encoding, 'replace')
    else:
        raise ValueError('basestring type required')


def ts2dt(ts, milli=False, tz_aware=True):
    ''' convert timestamp int's (seconds) to datetime objects '''
    # anything already a datetime will still be returned
    # tz_aware, if set to true
    if not ts or ts != ts:
        return None  # its not a timestamp
    elif isinstance(ts, datetime):
        pass
    elif milli:
        ts = float(ts) / 1000.  # convert milli to seconds
    else:
        ts = float(ts)  # already in seconds
    if tz_aware:
        if isinstance(ts, datetime):
            ts.replace(tzinfo=UTC)
            return ts
        else:
            return datetime.fromtimestamp(ts, tz=UTC)
    else:
        if isinstance(ts, datetime):
            return ts
        else:
            return datetime.utcfromtimestamp(ts)


def utcnow(as_datetime=True, tz_aware=False, drop_micro=False):
    if tz_aware:
        now = datetime.now(pytz.UTC)
    else:
        now = datetime.utcnow()
    if drop_micro:
        now = now.replace(microsecond=0)
    if as_datetime:
        return now
    else:
        return dt2ts(now, drop_micro)


def rupdate(d, u):
    ''' recursively update nested dictionaries
        see: http://stackoverflow.com/a/3233356/1289080
    '''
    for k, v in u.iteritems():
        if isinstance(v, collections.Mapping):
            r = rupdate(d.get(k, {}), v)
            d[k] = r
        else:
            d[k] = u[k]
    return d
