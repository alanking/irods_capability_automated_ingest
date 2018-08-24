import os
from os.path import dirname, getsize, getmtime, basename
from irods.session import iRODSSession
from irods.models import Resource, DataObject, Collection
import importlib
from irods_capability_automated_ingest.sync_utils import size
from irods_capability_automated_ingest import sync_logging
from irods_capability_automated_ingest.utils import Operation
import irods.keywords as kw
import json


def call(hdlr_mod, hdlr, func, logger, *args, **options):
    if hasattr(hdlr_mod, hdlr):
        logger.debug("calling " + hdlr + " event handler: args = " + str(args) + ", options = " + str(options))
        getattr(hdlr_mod, hdlr)(func, *args, **options)
    else:
        func(*args, **options)


def child_of(session, child_resc_name, resc_name):
    if child_resc_name == resc_name:
        return True
    else:
        while True:
            child_resc = session.resources.get(child_resc_name)
            parent_resc_id = child_resc.parent
            if parent_resc_id is None:
                break

            for row in session.query(Resource.name).filter(Resource.id == parent_resc_id):
                parent_resc_name = row[Resource.name]
            if parent_resc_name == resc_name:
                return True
            child_resc_name = parent_resc_name
        return False

'''
def create_dirs(hdlr_mod, logger, session, target, path, **options):
    if target.startswith("/"):
        if not session.collections.exists(target):
            if target == "/":
                raise Exception("create_dirs: Cannot create root")
            create_dirs(hdlr_mod, logger, session, dirname(target), dirname(path), **options)

            def ccfunc(hdlr_mod, session, target, path, **options):
                logger.info("creating collection " + target)
                session.collections.create(target)

            call(hdlr_mod, "on_coll_create", ccfunc, logger, hdlr_mod, session, target, path, **options)
    else:
        raise Exception("create_dirs: relative path")
'''


def create_dir(hdlr_mod, logger, session, target, path, **options):
    logger.info("creating collection " + target)
    session.collections.create(target)


def get_target_path(hdlr_mod, session, target, path, **options):
    if hasattr(hdlr_mod, "target_path"):
        return hdlr_mod.target_path(session, target, path, **options)
    else:
        return None


def get_resource_name(hdlr_mod, session, target, path, **options):
    if hasattr(hdlr_mod, "to_resource"):
        return hdlr_mod.to_resource(session, target, path, **options)
    else:
        return None


def register_file(hdlr_mod, logger, session, target, path, **options):
    target_path = get_target_path(hdlr_mod, session, target, path, **options)
    if target_path is None:
        target_path = path

    resc_name = get_resource_name(hdlr_mod, session, target, path, **options)

    if resc_name is not None:
        options["destRescName"] = resc_name

    size = getsize(path)
    mtime = int(getmtime(path))
    options[kw.DATA_SIZE_KW] = str(size)
    options[kw.DATA_MODIFY_KW] = str(mtime)

    logger.info("registering object " + target + ", options = " + str(options))
    session.data_objects.register(target_path, target, **options)

    logger.info("succeeded", task="irods_register_file", path = path)

def upload_file(hdlr_mod, logger, session, target, path, **options):
    resc_name = get_resource_name(hdlr_mod, session, target, path, **options)

    if resc_name is not None:
        options["destRescName"] = resc_name

    logger.info("uploading object " + target + ", options = " + str(options))
    session.data_objects.put(path, target, **options)
    logger.info("succeeded", task="irods_upload_file", path = path)


def no_op(hdlr_mod, logger, session, target, path, **options):
    pass


def sync_file(hdlr_mod, logger, session, target, path, **options):
    logger.info("syncing object " + target + ", options = " + str(options))

    resc_name = get_resource_name(hdlr_mod, session, target, path, **options)

    if resc_name is not None:
        options["destRescName"] = resc_name

    op = hdlr_mod.operation(session, target, path, **options)

    if op == Operation.PUT_APPEND:
        BUFFER_SIZE = 1024
        logger.info("appending object " + target + ", options = " + str(options))
        tsize = size(session, target)
        tfd = session.data_objects.open(target, "a", **options)
        tfd.seek(tsize)
        with open(path, "rb") as sfd:
            sfd.seek(tsize)
            while True:
                buf = sfd.read(BUFFER_SIZE)
                if buf == b"":
                    break
                tfd.write(buf)
        tfd.close()
        logger.info("succeeded", task="irods_append_file", path=path)

    else:
        logger.info("uploading object " + target + ", options = " + str(options))
        session.data_objects.put(path, target, **options)
        logger.info("succeeded", task="irods_update_file", path = path)


def update_metadata(hdlr_mod, logger, session, target, path, **options):
    target_path = get_target_path(hdlr_mod, session, target, path, **options)
    if target_path is None:
        target_path = path
    size = getsize(path)
    mtime = int(getmtime(path))
    logger.info("updating object: " + target + ", options = " + str(options))

    data_obj_info = {"objPath": target}

    resc_name = get_resource_name(hdlr_mod, session, target, path, **options)
    outdated_repl_nums = []
    found = False
    if resc_name is None:
        found = True
    else:
        for row in session.query(Resource.name, DataObject.path, DataObject.replica_number).filter(DataObject.name == basename(target), Collection.name == dirname(target)):
            if row[DataObject.path] == target_path:
                if child_of(session, row[Resource.name], resc_name):
                    found = True
                    repl_num = row[DataObject.replica_number]
                    data_obj_info["replNum"] = repl_num
                    continue
            outdated_repl_nums.append(row[DataObject.replica_number])

    if not found:
        logger.error("updating object: wrong resource or path, target = " + target + ", path = " + path + ", target_path = " + target_path + ", options = " + str(options))
        raise Exception("wrong resource or path")

    session.data_objects.modDataObjMeta(data_obj_info, {"dataSize":size, "dataModify":mtime, "replStatus":1}, **options)

    outdated_data_obj_info = {"objPath": target}

    for outdated_repl_num in outdated_repl_nums:
        outdated_data_obj_info["replNum"] = outdated_repl_num
        session.data_objects.modDataObjMeta(outdated_data_obj_info, {"replStatus":0})
    logger.info("succeeded", task="irods_update_metadata", path = path)


def sync_file_meta(hdlr_mod, logger, session, target, path, **options):
    pass


def sync_dir_meta(hdlr_mod, logger, session, target, path, **options):
    pass


irods_session_map = {}


def irods_session(hdlr_mod, target, path, **options):
    env_irods_host = os.environ.get("IRODS_HOST")
    env_irods_port = os.environ.get("IRODS_PORT")
    env_irods_user_name = os.environ.get("IRODS_USER_NAME")
    env_irods_zone_name = os.environ.get("IRODS_ZONE_NAME")
    env_irods_password = os.environ.get("IRODS_PASSWORD")

    env_file = os.environ.get('IRODS_ENVIRONMENT_FILE')

    kwargs = {}
    if env_irods_host is None or \
            env_irods_port is None or \
            env_irods_user_name is None or \
            env_irods_zone_name is None or \
            env_irods_password is None:
        if env_file is None:
            env_file = os.path.expanduser('~/.irods/irods_environment.json')

        kwargs["irods_env_file"] = env_file
    else:
        kwargs["host"] = env_irods_host
        kwargs["port"] = env_irods_port
        kwargs["user"] = env_irods_user_name
        kwargs["zone"] = env_irods_zone_name
        kwargs["password"] = env_irods_password

    if hasattr(hdlr_mod, "as_user"):
        client_zone, client_user = hdlr_mod.as_user(target, path, **options)
        kwargs["client_user"] = client_user
        kwargs["client_zone"] = client_zone

    key = json.dumps(kwargs) # todo add timestamp of env file to key

    sess = irods_session_map.get(key)

    if sess is not None:
        sess = iRODSSession(**kwargs)
        irods_session_map[key] = sess

    return sess

def sync_data_from_file(target, path, hdlr, logger, content, **options):
    if hdlr is not None:
        hdlr_mod0 = importlib.import_module(hdlr)
        hdlr_mod = getattr(hdlr_mod0, "event_handler", None)
    else:
        hdlr_mod = None

    session = irods_session(hdlr_mod, meta, **options)

    if session.data_objects.exists(target):
        exists = True
    elif session.collections.exists(target):
        raise Exception("sync: cannot syncing file " + path + " to collection " + target)
    else:
        exists = False

    if hasattr(hdlr_mod, "operation"):
        op = hdlr_mod.operation(session, target, path, **options)
    else:
        op = Operation.REGISTER_SYNC

    if op == Operation.NO_OP:
        if not exists:
            call(hdlr_mod, "on_data_obj_create", no_op, logger, hdlr_mod, logger, session, target, path, **options)
        else:
            call(hdlr_mod, "on_data_obj_modify", no_op, logger, hdlr_mod, logger, session, target, path, **options)
    else:
        createRepl = False
        if exists and op == Operation.REGISTER_AS_REPLICA_SYNC:
            if hasattr(hdlr_mod, "to_resource"):
                resc_name = hdlr_mod.to_resource(session, target, path, **options)
            else:
                raise Exception("no resource name defined")

            found = False
            foundPath = False
            for replica in session.data_objects.get(target).replicas:
                if child_of(session, replica.resource_name, resc_name):
                    found = True
                    if replica.path == path:
                        foundPath = True
            if found:
                if not foundPath:
                    raise Exception("there is at least one replica under resource but all replicas have wrong paths")
            else:
                createRepl = True

        put = op in [Operation.PUT, Operation.PUT_SYNC, Operation.PUT_APPEND]
        sync = op in [Operation.PUT_SYNC, Operation.PUT_APPEND]

        if not exists:
            if put:
                call(hdlr_mod, "on_data_obj_create", upload_file, logger, hdlr_mod, logger, session, target, path, **options)
            else:
                call(hdlr_mod, "on_data_obj_create", register_file, logger, hdlr_mod, logger, session, target, path, **options)
        elif createRepl:
            options["regRepl"] = ""

            call(hdlr_mod, "on_data_obj_create", register_file, logger, hdlr_mod, logger, session, target, path, **options)
        elif content:
            if put:
                if sync:
                    call(hdlr_mod, "on_data_obj_modify", sync_file, logger, hdlr_mod, logger, session, target, path, **options)
            else:
                call(hdlr_mod, "on_data_obj_modify", update_metadata, logger, hdlr_mod, logger, session, target, path, **options)
        else:
            call(hdlr_mod, "on_data_obj_modify", sync_file_meta, logger, hdlr_mod, logger, session, target, path, **options)


def sync_metadata_from_file(target, path, hdlr, logger, **options):
    sync_data_from_file(target, path, hdlr, logger, False, **options)


def sync_data_from_dir(target, path, hdlr, logger, content, **options):
    if hdlr is not None:
        hdlr_mod0 = importlib.import_module(hdlr)
        hdlr_mod = getattr(hdlr_mod0, "event_handler", None)
    else:
        hdlr_mod = None

    session = irods_session(hdlr_mod, meta, **options)

    exists = session.collections.exists(target)

    if hasattr(hdlr_mod, "operation"):
        op = hdlr_mod.operation(session, target, path, **options)
    else:
        op = Operation.REGISTER_SYNC

    if op == Operation.NO_OP:
        if not exists:
            call(hdlr_mod, "on_collection_create", no_op, logger, hdlr_mod, logger, session, target, path, **options)
        else:
            call(hdlr_mod, "on_collection_modify", no_op, logger, hdlr_mod, logger, session, target, path, **options)
    else:
        if not exists:
            call(hdlr_mod, "on_collection_create", create_dir, logger, hdlr_mod, logger, session, target, path, **options)
        else:
            call(hdlr_mod, "on_collection_modify", sync_dir_meta, logger, hdlr_mod, logger, session, target, path, **options)


def sync_metadata_from_dir(target, path, hdlr, logger, **options):
    sync_data_from_dir(target, path, hdlr, logger, False, **options)
