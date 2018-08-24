from os.path import dirname, basename
from irods.models import Collection, DataObject
from redis import StrictRedis, ConnectionPool
from irods_capability_automated_ingest import sync_logging
from rq import Queue, requeue_job
from rq.handlers import move_to_failed_queue
import importlib

def size(session, path, replica_num = None, resc_name = None):
    args = [Collection.name == dirname(path), DataObject.name == basename(path)]

    if replica_num is not None:
        args.append(DataObject.replica_number == replica_num)

    if resc_name is not None:
        args.append(DataObject.resource_name == resc_name)

    for row in session.query(DataObject.size).filter(*args):
        return int(row[DataObject.size])


redis_connection_pool_map = {}


def get_redis(config):
    redis_config = config["redis"]
    host = redis_config["host"]
    port = redis_config["port"]
    db = redis_config["db"]
    url = "redis://" + host + ":" + str(port) + "/" + str(db)
    pool = redis_connection_pool_map.get(url)
    if pool is None:
        pool = ConnectionPool(host=host, port=port, db=db)
        redis_connection_pool_map[url] = pool

    return StrictRedis(connection_pool=pool)


# based on https://gist.github.com/spjwebster/6521272

def retry_handler(job, exc_type, exc_value, traceback):
    try:
        config = job.meta['config']

        r = get_redis(config)

        logger = sync_logging.get_sync_logger()

        hdlr = job.meta.get("event_handler")

        if hdlr is not None:
            hdlr_mod0 = importlib.import_module(hdlr)
            hdlr_mod = getattr(hdlr_mod0, "event_handler", None)
        else:
            hdlr_mod = None

        if hasattr(hdlr_mod, "max_retries"):
            max_retries = hdlr_mod.max_retries(hdlr_mod, logger, job.meta["target"], job.meta["path"])
        else:
            max_retries = 0

        meta = job.meta
        meta.setdefault('failures', 0)
        meta['failures'] += 1
        job.save_meta()

        if meta['failures'] > max_retries:
            # Too many failures
            logger.warn('move_to_failed_queue', task=meta["task"], path=meta["path"], job_id=job.id, failures=meta["failures"], max_retries=max_retries)
            return True
        else:
            # Requeue job and stop it from being moved into the failed queue
            logger.warn('retry', task=meta["task"], path=meta["path"], job_id=job.id, failures=meta["failures"], max_retries=max_retries)
            requeue_job(job.id)
            return False
    except Exception as e:
        logger.error('retry', task=meta["task"], path=meta["path"], job_id=job.id, failures=meta["failures"], max_retries=max_retries, err=str(e))
        return True


