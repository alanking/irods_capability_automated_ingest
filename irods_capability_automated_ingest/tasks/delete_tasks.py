from .. import sync_logging
from ..celery import app, RestartTask
from ..custom_event_handler import custom_event_handler
from ..irods import irods_utils
from ..utils import enqueue_task
from .irods_task import IrodsTask

from irods.exception import (
    CollectionDoesNotExist,
    DataObjectDoesNotExist,
    PycommandsException,
)


def schedule_collections_for_removal(meta, list_of_collections_to_delete):
    if 0 == len(list_of_collections_to_delete):
        # This could be considered an error, but let's just treat it as a no-op.
        return
    meta_for_task = meta.copy()
    meta_for_task["queue_name"] = meta["path_queue"]
    meta_for_task["task"] = "delete_collection"
    for collection in list_of_collections_to_delete:
        meta_for_task["path"] = collection.path
        meta_for_task["target_collection"] = collection.path
        enqueue_task(delete_collection, meta_for_task)


def schedule_data_objects_for_removal(meta, list_of_objects_to_delete):
    if 0 == len(list_of_objects_to_delete):
        # This could be considered an error, but let's just treat it as a no-op.
        return
    meta_for_task = meta.copy()
    meta_for_task["queue_name"] = meta["file_queue"]
    meta_for_task["task"] = "delete_data_objects"
    removal_chunk = []
    chunk_size = meta_for_task.get("files_per_task", 50)
    for obj in list_of_objects_to_delete:
        removal_chunk.append(obj.path)
        if len(removal_chunk) == chunk_size:
            meta_for_task["data_objects_to_delete"] = removal_chunk
            enqueue_task(delete_data_objects, meta_for_task)
            removal_chunk = []
    if len(removal_chunk) > 0:
        meta_for_task["data_objects_to_delete"] = removal_chunk
        enqueue_task(delete_data_objects, meta_for_task)
        removal_chunk = []


@app.task(base=RestartTask)
def delete_collection_task(meta):
    logical_path = meta["target_collection"]
    meta_for_task = meta.copy()
    meta_for_task["queue_name"] = meta["path_queue"]
    meta_for_task["task"] = "delete_collection"
    meta_for_task["path"] = logical_path
    meta_for_task["target_collection"] = logical_path
    enqueue_task(delete_collection, meta_for_task)


@app.task(bind=True, base=IrodsTask)
def delete_collection(self, meta):
    config = meta["config"]
    logging_config = config["log"]
    logger = sync_logging.get_sync_logger(logging_config)
    event_handler = custom_event_handler(meta)
    logical_path = meta["target_collection"]
    session = irods_utils.irods_session(event_handler.get_module(), meta, logger)
    meta_for_task = meta.copy()
    meta_for_task["task"] = "delete_collection"
    try:
        target_collection = session.collections.get(logical_path)
    except CollectionDoesNotExist:
        # Print an error message here because the exception doesn't tell you what doesn't exist.
        logger.error(f"Collection [{logical_path}] does not exist.")
        raise
    if 0 == len(target_collection.data_objects) and 0 == len(
        target_collection.subcollections
    ):
        logger.debug(f"Removing empty collection [{target_collection.path}].")
        meta_for_task["target"] = target_collection.path
        irods_utils.delete_collection(event_handler.get_module(), meta_for_task)
        return
    schedule_data_objects_for_removal(meta, target_collection.data_objects)
    schedule_collections_for_removal(meta, target_collection.subcollections)
    # TODO: Consider doing this synchronously...
    schedule_collections_for_removal(meta, [target_collection])


@app.task(bind=True, base=IrodsTask)
def delete_data_objects(self, meta):
    config = meta["config"]
    logging_config = config["log"]
    logger = sync_logging.get_sync_logger(logging_config)
    meta_for_task = meta.copy()
    meta_for_task["task"] = "delete_data_object"
    logical_paths = meta_for_task["data_objects_to_delete"]
    if 0 == len(logical_paths):
        logger.warning("No data objects specified for removal - nothing to do.")
        return
    event_handler = custom_event_handler(meta)
    session = irods_utils.irods_session(event_handler.get_module(), meta, logger)
    for logical_path in logical_paths:
        try:
            meta_for_task["target"] = logical_path
            irods_utils.delete_data_object(event_handler.get_module(), meta_for_task)
        except DataObjectDoesNotExist:
            logger.error(
                f"Data object [{logical_path}] does not exist, so it cannot be delete."
            )
            continue
        except PycommandsException as e:
            logger.error(
                f"Exception occurred while removing data object [{logical_path}]: {e}"
            )
            continue
    # Synchronously attempt to delete the parent collection. Another task may have already done this depending on the
    # order of completion, or the collection may not be empty yet because there are more things being delete. The
    # parent collection will be delete either by a data object removal task or a subcollection removal task.
    parent_collection_path = meta.get("delete_empty_parent_collection")
    if parent_collection_path:
        logger.debug(
            f"Attempting to delete parent collection [{parent_collection_path}]."
        )
        meta_for_delete = meta.copy()
        meta_for_delete["target"] = parent_collection_path
        try:
            irods_utils.delete_collection(event_handler.get_module(), meta_for_delete)
        except CollectionDoesNotExist:
            logger.warning(
                f"Failed to delete parent collection [{parent_collection_path}]: it no longer exists."
            )
