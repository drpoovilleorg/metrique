#!/usr/bin/env python
# vim: tabstop=4 expandtab shiftwidth=4 softtabstop=4
# Author: "Chris Ward <cward@redhat.com>

import logging
logger = logging.getLogger(__name__)

from datetime import datetime
import hashlib
import re

from metriqued.cubes import get_collection, get_etl_activity
from metriqued.config import mongodb

from metriqued.utils import dt2ts
from metriqued.utils import new_oid

# obj props (client mutable) are prefixed w/ 1 underscore
RE_PROP = re.compile('^_')

mongodb_config = mongodb()
sha1 = hashlib.sha1
ETL_ACTIVITY = get_etl_activity()


def drop_cube(owner, cube):
    '''
    :param str cube: target cube (collection) to save objects to

    Wraps pymongo's drop() for the given cube (collection)
    '''
    get_collection(owner, cube, admin=True).drop()
    return True


def index(owner, cube, ensure=None, drop=None):
    '''
    :param str cube:
        name of cube (collection) to index
    :param string/list ensure:
        Either a single key or a list of (key, direction) pairs (lists)
        to ensure index on.
    :param string/list drop:
        index (or name of index) to drop
    '''
    _cube = get_collection(owner, cube, admin=True)

    if drop is not None:
        # when drop is a list of tuples, the json
        # serialization->deserialization process leaves us with a list of
        # lists, so we need to convert it back to a list of tuples.
        drop = map(tuple, drop) if isinstance(drop, list) else drop

        # FIXME: CHECK THAT DROP DOES NOT CONTAIN ANY _id or _oid_...
        # SYSTEM DEFAULT (IMMUTABLE!) INDEXES!

        _cube.drop_index(drop)

    if ensure is not None:
        # same as for drop:
        ensure = map(tuple, ensure) if isinstance(ensure, list) else ensure
        _cube.ensure_index(ensure)

    return _cube.index_information()


def _bulk_insert(_cube, docs, size=1000):
    for i in range(0, len(docs), size):
        _cube.insert(docs[i:i + size], manipulate=False)


def jsonhash(obj):
    '''
    calculate the objects hash based on all field values
    '''
    if isinstance(obj, dict):
        return sha1(
            repr(frozenset(
                dict(
                    [(jsonhash(k), jsonhash(v)) for k, v in obj.items()]
                ).items()
            ))
        ).hexdigest()
    elif isinstance(obj, (list, tuple, set)):
        return sha1(
            repr(tuple(
                sorted(
                    [jsonhash(e) for e in list(obj)]
                )
            ))
        ).hexdigest()
    else:
        return repr(obj)


def _prep_object(obj, mtime):
    '''
    :param dict obj: dictionary that will be converted to mongodb doc
    :param int mtime: timestamp to apply as _start for objects

    Do some basic object validatation and add an _start timestamp value
    '''
    if not isinstance(obj, dict):
        raise TypeError(
            "Expected dict object, got type(%s)."
            "\nObject: %s" % (type(obj), obj))

    if '_oid' not in obj:
        raise ValueError('Object must have an _oid specified.')

    obj['_hash'] = jsonhash(obj)

    # no object should have _mtime, we use _start
    if '_mtime' in obj:
        obj['_start'] = obj.pop('_mtime')

    if '_start' not in obj:
        # add the time when the object was last manipulated,
        # if one isn't already included
        obj['_start'] = mtime

    if not isinstance(obj['_start'], (int, long, float, complex)):
        raise TypeError(
            'Expected "numerical" type, got: %s' % type(obj['_start']))

    return obj


def _save_and_snapshot(_cube, objects):
    '''
    Each object in objects must have '_oid' and '_start' fields specified
    and it can *not* have fields '_end' and '_id' specified.
    In timeline(TL), the most recent version of an object has _end == None.
    For each object this method tries to find the most recent version of it
    in TL. If there is one, if the field-values specified in the new object
    are different than those in th object from TL, it will end the old object
    and insert the new one (fields that are not specified in the new object
    are copied from the old one).
    If there is not a version of the object in TL, it will just insert it.

    :param pymongo.collection _cube:
        cube object (pymongo collection connection)
    :param list objects:
        list of dictionary-like objects
    '''
    logger.debug('... Timeline Index: Start')
    _cube.ensure_index([('_oid', 1), ('_end', 1)])
    _cube.ensure_index([('_oid', 1), ('_start', 1)])
    logger.debug('... Timeline Index: Done')

    logger.debug('... To snapshot: %s objects.' % len(objects))

    # py2.7 syntax; fails with 2.6
    #docmap = {doc['_oid']: doc for doc in objects}
    # py2.6 compatibility
    docmap = dict([(doc['_oid'], doc) for doc in objects])
    logger.debug('... To snapshot: %s objects.' % len(docmap))

    time_docs = _cube.find({'_oid': {'$in': docmap.keys()}, '_end': None})

    for time_doc in time_docs:
        _oid = time_doc['_oid']
        try:
            doc = docmap[_oid]
        except KeyError:
            logger.warn('Document with _oid %s has more than one version with'
                        'end==None. Please repair your document.' % _oid)
            continue
        _start = doc.pop('_start')

        time_doc_items = time_doc.items()
        if any(item not in time_doc_items for item in doc.iteritems()):
            # document changed
            _cube.update({'_id': time_doc['_id']},
                         {'$set': {'_end': _start}},
                         upsert=True, manipulate=False)
            # from time_doc, we must copy the fields that are not present
            # in doc (in the case when we want to update only some of the
            # fields)
            time_doc.update(doc)
            time_doc['_start'] = _start
            docmap[_oid] = time_doc
        else:
            # document did not change
            docmap.pop(_oid)

    [doc.update({'_id': new_oid(), '_end': None}) for doc in docmap.values()]

    if docmap:
        _bulk_insert(_cube, docmap.values())
    logger.debug('... Snapped %s new versions.' % len(docmap))


def _save_no_snapshot(_cube, objects):
    '''
    Save all the objects (docs) into the given cube (mongodb collection)
    Each object must have '_oid', '_start', '_end' fields.
    The '_id' field is voluntary and its presence or absence determines
    the save method (see below).

    Use `save` to overwrite the entire document with the new version
    or `insert` when we have a document without a _id, indicating
    it's a new document, rather than an update of an existing doc.

    :param pymongo.collection _cube:
        cube object (pymongo collection connection)
    :param list objects:
        list of dictionary-like objects
    '''
    # save rather than insert b/c insert would add dups (_id) docs
    # if for object's we've already stored
    # maybe 'insert' only objects which don't have
    # and _id
    logger.debug('... No snapshot %s objects.' % len(objects))

    batch = []
    for obj in iter(objects):
        if '_id' in obj:
            _cube.save(obj, manipulate=False)
        else:
            obj.update({'_id': new_oid()})
            batch.append(obj)
    if batch:
        _cube.insert(batch, manipulate=False)


def _save_objects(owner, cube, objects):
    '''
    Save all the objects (docs) into the given cube (mongodb collection)
    Each object must have '_oid' and '_start' fields.
    If an object has an '_end' field, it will be saved without snapshot,
    otherwise it will be saved with snapshot.
    The '_id' field is allowed only if the object also has the '_end' field
    and its presence or absence determines the save method.


    :param pymongo.collection _cube:
        cube object (pymongo collection connection)
    :param list objects:
        list of dictionary-like objects
    '''
    _cube = get_collection(owner, cube, admin=True)

    fields = set()
    [fields.add(k) for doc in objects for k in doc.keys()]
    fields = list(filter(lambda k: k[0] != '_', fields))
    # Split the objects based on the presence of '_end' field:
    no_snap = [obj for obj in objects if '_end' in obj]
    _save_no_snapshot(_cube, no_snap) if len(no_snap) > 0 else []
    to_snap = [obj for obj in objects if '_end' not in obj]
    _save_and_snapshot(_cube, to_snap) if len(to_snap) > 0 else []
    return fields


def save_objects(owner, cube, objects, mtime=None):
    '''
    :param str owner: target owner's cube
    :param str cube: target cube (collection) to save objects to
    :param list objects: list of dictionary-like objects to be stored
    :param datetime mtime: datetime to apply as mtime for objects
    :rtype: list - list of object ids saved

    Get a list of dictionary objects from client and insert
    or save them to the timeline.

    Apply the given mtime to all objects or apply utcnow(). _mtime
    is used to support timebased 'delta' updates.
    '''
    if not (owner and cube and objects):
        raise ValueError('owner, cube, objects required')
    elif not isinstance(objects, list):
        raise TypeError("Expected list, got %s: %s" %
                        (type(objects), objects))

    # FIXME: CHECK IF CURRENT_USER CAN ACTUALLY WRITE TO USER"S CUBE

    mtime = dt2ts(mtime) if mtime else dt2ts(datetime.utcnow())

    objects = [_prep_object(obj, mtime) for obj in objects if obj]

    fields = _save_objects(owner, cube, objects)

    logger.debug('[%s.%s] Saved %s objects' % (owner, cube, len(objects)))

    # store info about which cube.fields got updated and when
    _etl = etl_activity_update(owner, cube, fields, mtime)
    logger.debug('ETL Activity Update: %s' % _etl)

    # return object ids saved
    try:
        oids = [o['_oid'] for o in objects]
    except KeyError:
        oids = []
    return oids


# FIXME: DO NOT PERMIT REMOVING (OR STORING) AND DOCUMENT WITH
# __meta__ key
def remove_objects(owner, cube, ids, backup=False):
    '''
    Remove all the objects (docs) from the given cube (mongodb collection)

    :param pymongo.collection _cube:
        cube object (pymongo collection connection)
    :param list ids:
        list of object ids
    '''
    if not ids:
        logger.debug('REMOVE: no ids provided')
        return []
    elif not isinstance(ids, list):
        raise TypeError("Expected list, got %s: %s" %
                        (type(ids), ids))
    else:
        spec = {'_oid': {'$in': ids}}
        _cube = get_collection(owner, cube)
        if backup:
            docs = _cube.find(spec)
            if docs:
                docs = tuple(docs)
        else:
            docs = []
        try:
            get_collection(owner, cube, admin=True).remove(spec, safe=True)
        except Exception as e:
            raise RuntimeError("Failed to remove docs: %s" % e)
        else:
            return docs


def etl_activity_update(owner, cube, fields, mtime):
    '''
    :param str cube: target cube (collection) to save objects to
    :param list fields: list fields updated
    :param datetime mtime: datetime to apply as mtime for objects

    Update etl_activity collection in metrique mongodb with
    information about which cube.fields have been manipulated
    and when.
    '''
    fields = list(set(fields))
    collection = '%s__%s' % (owner, cube)
    spec = {'_id': collection}
    mtimes = dict([(f, mtime) for f in fields if not RE_PROP.match(f)])
    mtimes.update({'_mtime': mtime})
    update = {'$set': mtimes}
    return ETL_ACTIVITY.update(spec, update, upsert=True, safe=True)
