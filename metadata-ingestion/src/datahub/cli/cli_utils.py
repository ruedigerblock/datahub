import json
import logging
import os
import os.path
import sys
import typing
from datetime import datetime
from typing import Any, Dict, Iterable, List, Optional, Tuple, Type, Union

import click
import requests
import yaml
from pydantic import BaseModel, ValidationError
from requests.models import Response
from requests.sessions import Session

from datahub.emitter.aspect import ASPECT_MAP, TIMESERIES_ASPECT_MAP
from datahub.emitter.request_helper import _make_curl_command
from datahub.emitter.serialization_helper import post_json_transform
from datahub.metadata.schema_classes import _Aspect
from datahub.utilities.urns.urn import Urn, guess_entity_type

log = logging.getLogger(__name__)

DEFAULT_GMS_HOST = "http://localhost:8080"
CONDENSED_DATAHUB_CONFIG_PATH = "~/.datahubenv"
DATAHUB_CONFIG_PATH = os.path.expanduser(CONDENSED_DATAHUB_CONFIG_PATH)

DATAHUB_ROOT_FOLDER = os.path.expanduser("~/.datahub")

ENV_SKIP_CONFIG = "DATAHUB_SKIP_CONFIG"
ENV_METADATA_HOST_URL = "DATAHUB_GMS_URL"
ENV_METADATA_HOST = "DATAHUB_GMS_HOST"
ENV_METADATA_PORT = "DATAHUB_GMS_PORT"
ENV_METADATA_PROTOCOL = "DATAHUB_GMS_PROTOCOL"
ENV_METADATA_TOKEN = "DATAHUB_GMS_TOKEN"
ENV_DATAHUB_SYSTEM_CLIENT_ID = "DATAHUB_SYSTEM_CLIENT_ID"
ENV_DATAHUB_SYSTEM_CLIENT_SECRET = "DATAHUB_SYSTEM_CLIENT_SECRET"

config_override: Dict = {}

# TODO: Many of the methods in this file duplicate logic that already lives
# in the DataHubGraph client. We should refactor this to use the client instead.
# For the methods that aren't duplicates, that logic should be moved to the client.


class GmsConfig(BaseModel):
    server: str
    token: Optional[str]


class DatahubConfig(BaseModel):
    gms: GmsConfig


def get_boolean_env_variable(key: str, default: bool = False) -> bool:
    value = os.environ.get(key)
    if value is None:
        return default
    elif value.lower() in ("true", "1"):
        return True
    else:
        return False


def set_env_variables_override_config(url: str, token: Optional[str]) -> None:
    """Should be used to override the config when using rest emitter"""
    config_override[ENV_METADATA_HOST_URL] = url
    if token is not None:
        config_override[ENV_METADATA_TOKEN] = token


def persist_datahub_config(config: dict) -> None:
    with open(DATAHUB_CONFIG_PATH, "w+") as outfile:
        yaml.dump(config, outfile, default_flow_style=False)
    return None


def write_gms_config(
    host: str, token: Optional[str], merge_with_previous: bool = True
) -> None:
    config = DatahubConfig(gms=GmsConfig(server=host, token=token))
    if merge_with_previous:
        try:
            previous_config = get_client_config(as_dict=True)
            assert isinstance(previous_config, dict)
        except Exception as e:
            # ok to fail on this
            previous_config = {}
            log.debug(
                f"Failed to retrieve config from file {DATAHUB_CONFIG_PATH}. This isn't fatal.",
                e,
            )
        config_dict = {**previous_config, **config.dict()}
    else:
        config_dict = config.dict()
    persist_datahub_config(config_dict)


def should_skip_config() -> bool:
    return get_boolean_env_variable(ENV_SKIP_CONFIG, False)


def ensure_datahub_config() -> None:
    if not os.path.isfile(DATAHUB_CONFIG_PATH):
        click.secho(
            f"No {CONDENSED_DATAHUB_CONFIG_PATH} file found, generating one for you...",
            bold=True,
        )
        write_gms_config(DEFAULT_GMS_HOST, None)


def get_client_config(as_dict: bool = False) -> Union[Optional[DatahubConfig], dict]:
    with open(DATAHUB_CONFIG_PATH, "r") as stream:
        try:
            config_json = yaml.safe_load(stream)
            if as_dict:
                return config_json
            try:
                datahub_config = DatahubConfig.parse_obj(config_json)
                return datahub_config
            except ValidationError as e:
                click.echo(
                    f"Received error, please check your {CONDENSED_DATAHUB_CONFIG_PATH}"
                )
                click.echo(e, err=True)
                sys.exit(1)
        except yaml.YAMLError as exc:
            click.secho(f"{DATAHUB_CONFIG_PATH} malformed, error: {exc}", bold=True)
            return None


def get_details_from_config():
    datahub_config = get_client_config(as_dict=False)
    assert isinstance(datahub_config, DatahubConfig)
    if datahub_config is not None:
        gms_config = datahub_config.gms

        gms_host = gms_config.server
        gms_token = gms_config.token
        return gms_host, gms_token
    else:
        return None, None


def get_details_from_env() -> Tuple[Optional[str], Optional[str]]:
    host = os.environ.get(ENV_METADATA_HOST)
    port = os.environ.get(ENV_METADATA_PORT)
    token = os.environ.get(ENV_METADATA_TOKEN)
    protocol = os.environ.get(ENV_METADATA_PROTOCOL, "http")
    url = os.environ.get(ENV_METADATA_HOST_URL)
    if port is not None:
        url = f"{protocol}://{host}:{port}"
        return url, token
    # The reason for using host as URL is backward compatibility
    # If port is not being used we assume someone is using host env var as URL
    if url is None and host is not None:
        log.warning(
            f"Do not use {ENV_METADATA_HOST} as URL. Use {ENV_METADATA_HOST_URL} instead"
        )
    return url or host, token


def first_non_null(ls: List[Optional[str]]) -> Optional[str]:
    return next((el for el in ls if el is not None and el.strip() != ""), None)


def get_system_auth() -> Optional[str]:
    system_client_id = os.environ.get(ENV_DATAHUB_SYSTEM_CLIENT_ID)
    system_client_secret = os.environ.get(ENV_DATAHUB_SYSTEM_CLIENT_SECRET)
    if system_client_id is not None and system_client_secret is not None:
        return f"Basic {system_client_id}:{system_client_secret}"
    return None


def get_url_and_token():
    gms_host_env, gms_token_env = get_details_from_env()
    if len(config_override.keys()) > 0:
        gms_host = config_override.get(ENV_METADATA_HOST_URL)
        gms_token = config_override.get(ENV_METADATA_TOKEN)
    elif should_skip_config():
        gms_host = gms_host_env
        gms_token = gms_token_env
    else:
        ensure_datahub_config()
        gms_host_conf, gms_token_conf = get_details_from_config()
        gms_host = first_non_null([gms_host_env, gms_host_conf])
        gms_token = first_non_null([gms_token_env, gms_token_conf])
    return gms_host, gms_token


def get_token():
    return get_url_and_token()[1]


def get_session_and_host():
    session = requests.Session()

    gms_host, gms_token = get_url_and_token()

    if gms_host is None or gms_host.strip() == "":
        log.error(
            f"GMS Host is not set. Use datahub init command or set {ENV_METADATA_HOST_URL} env var"
        )
        return None, None

    session.headers.update(
        {
            "X-RestLi-Protocol-Version": "2.0.0",
            "Content-Type": "application/json",
        }
    )
    if isinstance(gms_token, str) and len(gms_token) > 0:
        session.headers.update(
            {"Authorization": f"Bearer {gms_token.format(**os.environ)}"}
        )

    return session, gms_host


def test_connection():
    (session, host) = get_session_and_host()
    url = f"{host}/config"
    response = session.get(url)
    response.raise_for_status()


def test_connectivity_complain_exit(operation_name: str) -> None:
    """Test connectivity to metadata-service, log operation name and exit"""
    # First test connectivity
    try:
        test_connection()
    except Exception as e:
        click.secho(
            f"Failed to connect to DataHub server at {get_session_and_host()[1]}. Run with datahub --debug {operation_name} ... to get more information.",
            fg="red",
        )
        log.debug(f"Failed to connect with {e}")
        sys.exit(1)


def parse_run_restli_response(response: requests.Response) -> dict:
    response_json = response.json()
    if response.status_code != 200:
        if isinstance(response_json, dict):
            if "message" in response_json:
                click.secho("Failed to execute operation", fg="red")
                click.secho(f"{response_json['message']}", fg="red")
            else:
                click.secho(f"Failed with \n{response_json}", fg="red")
        else:
            response.raise_for_status()
        exit()

    if not isinstance(response_json, dict):
        click.echo(f"Received error, please check your {CONDENSED_DATAHUB_CONFIG_PATH}")
        click.echo()
        click.echo(response_json)
        exit()

    summary = response_json.get("value")
    if not isinstance(summary, dict):
        click.echo(f"Received error, please check your {CONDENSED_DATAHUB_CONFIG_PATH}")
        click.echo()
        click.echo(response_json)
        exit()

    return summary


def format_aspect_summaries(summaries: list) -> typing.List[typing.List[str]]:
    local_timezone = datetime.now().astimezone().tzinfo
    return [
        [
            row.get("urn"),
            row.get("aspectName"),
            datetime.fromtimestamp(row.get("timestamp") / 1000).strftime(
                "%Y-%m-%d %H:%M:%S"
            )
            + f" ({local_timezone})",
        ]
        for row in summaries
    ]


def post_rollback_endpoint(
    payload_obj: dict,
    path: str,
) -> typing.Tuple[typing.List[typing.List[str]], int, int, int, int, typing.List[dict]]:
    session, gms_host = get_session_and_host()
    url = gms_host + path

    payload = json.dumps(payload_obj)
    response = session.post(url, payload)

    summary = parse_run_restli_response(response)
    rows = summary.get("aspectRowSummaries", [])
    entities_affected = summary.get("entitiesAffected", 0)
    aspects_reverted = summary.get("aspectsReverted", 0)
    aspects_affected = summary.get("aspectsAffected", 0)
    unsafe_entity_count = summary.get("unsafeEntitiesCount", 0)
    unsafe_entities = summary.get("unsafeEntities", [])
    rolled_back_aspects = list(
        filter(lambda row: row["runId"] == payload_obj["runId"], rows)
    )

    if len(rows) == 0:
        click.secho(f"No entities found. Payload used: {payload}", fg="yellow")

    structured_rolled_back_results = format_aspect_summaries(rolled_back_aspects)
    return (
        structured_rolled_back_results,
        entities_affected,
        aspects_reverted,
        aspects_affected,
        unsafe_entity_count,
        unsafe_entities,
    )


def post_delete_references_endpoint(
    payload_obj: dict,
    path: str,
    cached_session_host: Optional[Tuple[Session, str]] = None,
) -> Tuple[int, List[Dict]]:
    session, gms_host = cached_session_host or get_session_and_host()
    url = gms_host + path

    payload = json.dumps(payload_obj)
    response = session.post(url, payload)
    summary = parse_run_restli_response(response)
    reference_count = summary.get("total", 0)
    related_aspects = summary.get("relatedAspects", [])
    return reference_count, related_aspects


def post_delete_endpoint(
    payload_obj: dict,
    path: str,
    cached_session_host: Optional[Tuple[Session, str]] = None,
) -> typing.Tuple[str, int, int]:
    session, gms_host = cached_session_host or get_session_and_host()
    url = gms_host + path

    return post_delete_endpoint_with_session_and_url(session, url, payload_obj)


def post_delete_endpoint_with_session_and_url(
    session: Session,
    url: str,
    payload_obj: dict,
) -> typing.Tuple[str, int, int]:
    payload = json.dumps(payload_obj)

    response = session.post(url, payload)

    summary = parse_run_restli_response(response)
    urn: str = summary.get("urn", "")
    rows_affected: int = summary.get("rows", 0)
    timeseries_rows_affected: int = summary.get("timeseriesRows", 0)

    return urn, rows_affected, timeseries_rows_affected


def get_urns_by_filter(
    platform: Optional[str],
    env: Optional[str] = None,
    entity_type: str = "dataset",
    search_query: str = "*",
    include_removed: bool = False,
    only_soft_deleted: Optional[bool] = None,
) -> Iterable[str]:
    session, gms_host = get_session_and_host()
    endpoint: str = "/entities?action=search"
    url = gms_host + endpoint
    filter_criteria = []
    entity_type_lower = entity_type.lower()
    if env and entity_type_lower != "container":
        filter_criteria.append({"field": "origin", "value": env, "condition": "EQUAL"})
    if (
        platform is not None
        and entity_type_lower == "dataset"
        or entity_type_lower == "dataflow"
        or entity_type_lower == "datajob"
        or entity_type_lower == "container"
    ):
        filter_criteria.append(
            {
                "field": "platform",
                "value": f"urn:li:dataPlatform:{platform}",
                "condition": "EQUAL",
            }
        )
    if platform is not None and entity_type_lower in {"chart", "dashboard"}:
        filter_criteria.append(
            {
                "field": "tool",
                "value": platform,
                "condition": "EQUAL",
            }
        )

    if only_soft_deleted:
        filter_criteria.append(
            {
                "field": "removed",
                "value": "true",
                "condition": "EQUAL",
            }
        )
    elif include_removed:
        filter_criteria.append(
            {
                "field": "removed",
                "value": "",  # accept anything regarding removed property (true, false, non-existent)
                "condition": "EQUAL",
            }
        )

    search_body = {
        "input": search_query,
        "entity": entity_type,
        "start": 0,
        "count": 10000,
        "filter": {"or": [{"and": filter_criteria}]},
    }
    payload = json.dumps(search_body)
    log.debug(payload)
    response: Response = session.post(url, payload)
    if response.status_code == 200:
        assert response._content
        results = json.loads(response._content)
        num_entities = results["value"]["numEntities"]
        entities_yielded: int = 0
        for x in results["value"]["entities"]:
            entities_yielded += 1
            log.debug(f"yielding {x['entity']}")
            yield x["entity"]
        if entities_yielded != num_entities:
            log.warning(
                f"Discrepancy in entities yielded {entities_yielded} and num entities {num_entities}. This means all entities may not have been deleted."
            )
    else:
        log.error(f"Failed to execute search query with {str(response.content)}")
        response.raise_for_status()


def get_container_ids_by_filter(
    env: Optional[str],
    entity_type: str = "container",
    search_query: str = "*",
) -> Iterable[str]:
    session, gms_host = get_session_and_host()
    endpoint: str = "/entities?action=search"
    url = gms_host + endpoint

    container_filters = []
    for container_subtype in ["Database", "Schema", "Project", "Dataset"]:
        filter_criteria = []

        filter_criteria.append(
            {
                "field": "customProperties",
                "value": f"instance={env}",
                "condition": "EQUAL",
            }
        )

        filter_criteria.append(
            {
                "field": "typeNames",
                "value": container_subtype,
                "condition": "EQUAL",
            }
        )
        container_filters.append({"and": filter_criteria})
    search_body = {
        "input": search_query,
        "entity": entity_type,
        "start": 0,
        "count": 10000,
        "filter": {"or": container_filters},
    }
    payload = json.dumps(search_body)
    log.debug(payload)
    response: Response = session.post(url, payload)
    if response.status_code == 200:
        assert response._content
        log.debug(response._content)
        results = json.loads(response._content)
        num_entities = results["value"]["numEntities"]
        entities_yielded: int = 0
        for x in results["value"]["entities"]:
            entities_yielded += 1
            log.debug(f"yielding {x['entity']}")
            yield x["entity"]
        assert (
            entities_yielded == num_entities
        ), "Did not delete all entities, try running this command again!"
    else:
        log.error(f"Failed to execute search query with {str(response.content)}")
        response.raise_for_status()


def batch_get_ids(
    ids: List[str],
) -> Iterable[Dict]:
    session, gms_host = get_session_and_host()
    endpoint: str = "/entitiesV2"
    url = gms_host + endpoint
    ids_to_get = [Urn.url_encode(id) for id in ids]
    response = session.get(
        f"{url}?ids=List({','.join(ids_to_get)})",
    )

    if response.status_code == 200:
        assert response._content
        log.debug(response._content)
        results = json.loads(response._content)
        num_entities = len(results["results"])
        entities_yielded: int = 0
        for x in results["results"].values():
            entities_yielded += 1
            log.debug(f"yielding {x}")
            yield x
        assert (
            entities_yielded == num_entities
        ), "Did not delete all entities, try running this command again!"
    else:
        log.error(f"Failed to execute batch get with {str(response.content)}")
        response.raise_for_status()


def get_incoming_relationships(urn: str, types: List[str]) -> Iterable[Dict]:
    yield from get_relationships(urn=urn, types=types, direction="INCOMING")


def get_outgoing_relationships(urn: str, types: List[str]) -> Iterable[Dict]:
    yield from get_relationships(urn=urn, types=types, direction="OUTGOING")


def get_relationships(urn: str, types: List[str], direction: str) -> Iterable[Dict]:
    session, gms_host = get_session_and_host()
    encoded_urn: str = Urn.url_encode(urn)
    types_param_string = "List(" + ",".join(types) + ")"
    endpoint: str = f"{gms_host}/relationships?urn={encoded_urn}&direction={direction}&types={types_param_string}"
    response: Response = session.get(endpoint)
    if response.status_code == 200:
        results = response.json()
        log.debug(f"Relationship response: {results}")
        num_entities = results["count"]
        entities_yielded: int = 0
        for x in results["relationships"]:
            entities_yielded += 1
            yield x
        if entities_yielded != num_entities:
            log.warn("Yielded entities differ from num entities")
    else:
        log.error(f"Failed to execute relationships query with {str(response.content)}")
        response.raise_for_status()


def get_entity(
    urn: str,
    aspect: Optional[List] = None,
    cached_session_host: Optional[Tuple[Session, str]] = None,
) -> Dict:
    session, gms_host = cached_session_host or get_session_and_host()
    if urn.startswith("urn%3A"):
        # we assume the urn is already encoded
        encoded_urn: str = urn
    elif urn.startswith("urn:"):
        encoded_urn = Urn.url_encode(urn)
    else:
        raise Exception(
            f"urn {urn} does not seem to be a valid raw (starts with urn:) or encoded urn (starts with urn%3A)"
        )
    endpoint: str = f"/entitiesV2/{encoded_urn}"

    if aspect and len(aspect):
        endpoint = f"{endpoint}?aspects=List(" + ",".join(aspect) + ")"

    response = session.get(gms_host + endpoint)
    response.raise_for_status()
    return response.json()


def post_entity(
    urn: str,
    entity_type: str,
    aspect_name: str,
    aspect_value: Dict,
    cached_session_host: Optional[Tuple[Session, str]] = None,
    is_async: Optional[str] = "false",
) -> int:
    session, gms_host = cached_session_host or get_session_and_host()
    endpoint: str = "/aspects/?action=ingestProposal"

    proposal = {
        "proposal": {
            "entityType": entity_type,
            "entityUrn": urn,
            "aspectName": aspect_name,
            "changeType": "UPSERT",
            "aspect": {
                "contentType": "application/json",
                "value": json.dumps(aspect_value),
            },
        },
        "async": is_async,
    }
    payload = json.dumps(proposal)
    url = gms_host + endpoint
    curl_command = _make_curl_command(session, "POST", url, payload)
    log.debug(
        "Attempting to emit to DataHub GMS; using curl equivalent to:\n%s",
        curl_command,
    )
    response = session.post(url, payload)
    if not response.ok:
        try:
            log.info(response.json()["message"].strip())
        except Exception:
            log.info(f"post_entity failed: {response.text}")
    response.raise_for_status()
    return response.status_code


def _get_pydantic_class_from_aspect_name(aspect_name: str) -> Optional[Type[_Aspect]]:
    return ASPECT_MAP.get(aspect_name)


def get_latest_timeseries_aspect_values(
    entity_urn: str,
    timeseries_aspect_name: str,
    cached_session_host: Optional[Tuple[Session, str]],
) -> Dict:
    session, gms_host = cached_session_host or get_session_and_host()
    query_body = {
        "urn": entity_urn,
        "entity": guess_entity_type(entity_urn),
        "aspect": timeseries_aspect_name,
        "latestValue": True,
    }
    end_point = "/aspects?action=getTimeseriesAspectValues"
    try:
        response = session.post(url=gms_host + end_point, data=json.dumps(query_body))
        response.raise_for_status()
        return response.json()
    except Exception:
        # Ignore exceptions
        return {}


def get_aspects_for_entity(
    entity_urn: str,
    aspects: List[str] = [],
    typed: bool = False,
    cached_session_host: Optional[Tuple[Session, str]] = None,
) -> Dict[str, Union[dict, _Aspect]]:
    # Process non-timeseries aspects
    non_timeseries_aspects = [a for a in aspects if a not in TIMESERIES_ASPECT_MAP]
    entity_response = get_entity(
        entity_urn, non_timeseries_aspects, cached_session_host
    )
    aspect_list: Dict[str, dict] = entity_response["aspects"]

    # Process timeseries aspects & append to aspect_list
    timeseries_aspects: List[str] = [a for a in aspects if a in TIMESERIES_ASPECT_MAP]
    for timeseries_aspect in timeseries_aspects:
        timeseries_response: Dict = get_latest_timeseries_aspect_values(
            entity_urn, timeseries_aspect, cached_session_host
        )
        values: List[Dict] = timeseries_response.get("value", {}).get("values", [])
        if values:
            aspect_cls: Optional[Type] = _get_pydantic_class_from_aspect_name(
                timeseries_aspect
            )
            if aspect_cls is not None:
                ts_aspect = values[0]["aspect"]
                # Decode the json-encoded generic aspect value.
                ts_aspect["value"] = json.loads(ts_aspect["value"])
                aspect_list[timeseries_aspect] = ts_aspect

    aspect_map: Dict[str, Union[dict, _Aspect]] = {}
    for aspect_name, a in aspect_list.items():
        aspect_py_class: Optional[Type[Any]] = _get_pydantic_class_from_aspect_name(
            aspect_name
        )
        if aspect_name == "unknown":
            print(f"Failed to find aspect_name for class {aspect_name}")

        aspect_dict = a["value"]
        if not typed:
            aspect_map[aspect_name] = aspect_dict
        elif aspect_py_class:
            try:
                post_json_obj = post_json_transform(aspect_dict)
                aspect_map[aspect_name] = aspect_py_class.from_obj(post_json_obj)
            except Exception as e:
                log.error(f"Error on {json.dumps(aspect_dict)}", e)

    if aspects:
        return {k: v for (k, v) in aspect_map.items() if k in aspects}
    else:
        return dict(aspect_map)
