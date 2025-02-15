import logging
import re
import sys
from functools import partial
from functools import wraps
from string import Template
from typing import Callable
from typing import Dict
from typing import Iterable
from typing import List
from typing import Optional
from typing import Union

import botocore
import neo4j

from cartography.graph.job import GraphJob
from cartography.graph.statement import get_job_shortname
from cartography.stats import get_stats_client
from cartography.stats import ScopedStatsClient

if sys.version_info >= (3, 7):
    from importlib.resources import open_binary, read_text
else:
    from importlib_resources import open_binary, read_text

logger = logging.getLogger(__name__)


def run_analysis_job(filename, neo4j_session, common_job_parameters, package='cartography.data.jobs.analysis'):
    GraphJob.run_from_json(
        neo4j_session,
        read_text(
            package,
            filename,
        ),
        common_job_parameters,
        get_job_shortname(filename),
    )


def run_cleanup_job(
    filename: str, neo4j_session: neo4j.Session, common_job_parameters: Dict,
    package: str = 'cartography.data.jobs.cleanup',
) -> None:
    GraphJob.run_from_json(
        neo4j_session,
        read_text(
            package,
            filename,
        ),
        common_job_parameters,
        get_job_shortname(filename),
    )


def merge_module_sync_metadata(
    neo4j_session: neo4j.Session,
    group_type: str,
    group_id: Union[str, int],
    synced_type: str,
    update_tag: int,
    stat_handler: ScopedStatsClient,
):
    '''
    This creates `ModuleSyncMetadata` nodes when called from each of the individual modules or sub-modules.
    The 'types' used here should be actual node labels. For example, if we did sync a particular AWSAccount's S3Buckets,
    the `grouptype` is 'AWSAccount', the `groupid` is the particular account's `id`, and the `syncedtype` is 'S3Bucket'.

    :param neo4j_session: Neo4j session object
    :param group_type: The parent module's type
    :param group_id: The parent module's id
    :param synced_type: The sub-module's type
    :param update_tag: Timestamp used to determine data freshness
    '''
    template = Template("""
        MERGE (n:ModuleSyncMetadata{id:'${group_type}_${group_id}_${synced_type}'})
        ON CREATE SET
            n:SyncMetadata, n.firstseen=timestamp()
        SET n.syncedtype='${synced_type}',
            n.grouptype='${group_type}',
            n.groupid={group_id},
            n.lastupdated={UPDATE_TAG}
    """)
    neo4j_session.run(
        template.safe_substitute(group_type=group_type, group_id=group_id, synced_type=synced_type),
        group_id=group_id,
        UPDATE_TAG=update_tag,
    )
    stat_handler.incr(f'{group_type}_{group_id}_{synced_type}_lastupdated', update_tag)


def load_resource_binary(package, resource_name):
    return open_binary(package, resource_name)


def timeit(method):
    """
    This decorator uses statsd to time the execution of the wrapped method and sends it to the statsd server.
    This is only active if config.statsd_enabled is True.
    :param method: The function to measure execution
    """
    # Allow access via `inspect` to the wrapped function. This is used in integration tests to standardize param names.
    @wraps(method)
    def timed(*args, **kwargs):
        stats_client = get_stats_client(None)
        if stats_client.is_enabled():
            # Example metric name "cartography.intel.aws.iam.get_group_membership_data"
            metric_name = f"{method.__module__}.{method.__name__}"
            timer = stats_client.timer(metric_name)
            timer.start()
            result = method(*args, **kwargs)
            timer.stop()
            return result
        else:
            # statsd is disabled, so don't time anything
            return method(*args, **kwargs)

    return timed


# TODO Move this to cartography.intel.aws.util.common
def aws_handle_regions(func=None, default_return_value=[]) -> Callable:
    """
    A decorator for returning a default value on functions that would return a client error
     like AccessDenied for opt-in AWS regions, and other regions that might be disabled.

    The convenience of this decorator is that it auto-catches some of the potential
     Exceptions related to opt-in regions, and returns the specified `default_return_value`.

    This should be used on `get_` functions that normally return a list of items.
     but it can be used elsehwere and you can supply a custom `default_return_value`,
     other than a simple list `[]`.
    """
    ERROR_CODES = [
        'AccessDenied',
        'AccessDeniedException',
        'AuthFailure',
        'InvalidClientTokenId',
        'UnrecognizedClientException',
    ]

    @wraps(func)
    def wrapper(*args, **kwargs):
        try:
            return func(*args, **kwargs)
        except botocore.exceptions.ClientError as e:
            # The account is not authorized to use this service in this region
            # so we can continue without raising an exception
            if e.response['Error']['Code'] in ERROR_CODES:
                logger.warning("{} in this region. Skipping...".format(e.response['Error']['Message']))
                return default_return_value
            else:
                raise
    if func is None:
        return partial(aws_handle_regions, default_return_value=default_return_value)
    return wrapper


def dict_value_to_str(obj: Dict, key: str) -> Optional[str]:
    """
    Convert the value referenced by the key in the dict to a string, if it exists, and return it. If it doesn't exist,
    return None.
    """
    value = obj.get(key)
    if value is not None:
        return str(value)
    else:
        return None


def dict_date_to_epoch(obj: Dict, key: str) -> Optional[int]:
    """
    Convert the date referenced by the key in the dict to an epoch timestamp, if it exists, and return it. If it
    doesn't exist, return None.
    """
    value = obj.get(key)
    if value is not None:
        return int(value.timestamp())
    else:
        return None


def camel_to_snake(name: str) -> str:
    return re.sub(r'(?<!^)(?=[A-Z])', '_', name).lower()


def batch(items: Iterable, size=1000) -> List[List]:
    '''
    Takes an Iterable of items and returns a list of lists of the same items,
     batched into chunks of the provided `size`.

    Use:
    x = [1,2,3,4,5,6,7,8]
    batch(x, size=3) -> [[1, 2, 3], [4, 5, 6], [7, 8]]
    '''
    items = list(items)
    return [
        items[i: i + size]
        for i in range(0, len(items), size)
    ]
