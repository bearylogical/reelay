#!/usr/bin/env python3

import json
import logging

import requests

from . import commons
from . import logger
from .config import config

# Set up logging
logLevel = logging.DEBUG if config.get("debugLogging", False) else logging.INFO
logger = logger.getLogger("reelay.sonarr", logLevel, config.get("logToConsole", False))

config = config["sonarr"]

addSerieNeededFields = ["tvdbId", "tvRageId", "title", "titleSlug", "images", "seasons"]


def search(title):
    parameters = {"term": title}
    try:
        req = requests.get(commons.generateApiQuery("sonarr", "series/lookup", parameters))
        parsed_json = json.loads(req.text)
    except Exception as e:
        logger.warning(f"Sonarr search failed: {e}")
        return False

    if req.status_code == 200 and parsed_json:
        return parsed_json
    else:
        logger.warning(f"Sonarr search returned status={req.status_code} for {title!r}")
        return False


def giveTitles(parsed_json):
    data = []
    for show in parsed_json:
        if all(
            x in show
            for x in ["title", "statistics", "year", "tvdbId"]
        ):
            data.append(
                {
                    "title": show["title"],
                    "seasonCount": show["statistics"]["seasonCount"],
                    "poster": show.get("remotePoster", None),
                    "year": show["year"],
                    "id": show["tvdbId"],
                    "monitored": show["monitored"],
                    "status": show["status"],
                }
            )
    return data


def inLibrary(tvdbId):
    parameters = {}
    try:
        req = requests.get(commons.generateApiQuery("sonarr", "series", parameters))
        parsed_json = json.loads(req.text)
    except Exception as e:
        logger.warning(f"Sonarr inLibrary check failed: {e}")
        return False
    return next((True for show in parsed_json if show["tvdbId"] == tvdbId), False)


def addToLibrary(tvdbId, path, qualityProfileId, tags, seasonsSelected):
    parameters = {"term": "tvdb:" + str(tvdbId)}
    try:
        req = requests.get(commons.generateApiQuery("sonarr", "series/lookup", parameters))
        parsed_json = json.loads(req.text)
        data = json.dumps(buildData(parsed_json, path, qualityProfileId, tags, seasonsSelected))
        add = requests.post(commons.generateApiQuery("sonarr", "series"), data=data, headers={'Content-Type': 'application/json'})
    except Exception as e:
        logger.warning(f"Sonarr addToLibrary failed for tvdbId={tvdbId}: {e}")
        return False
    if add.status_code == 201:
        return True
    else:
        logger.warning(f"Sonarr addToLibrary rejected tvdbId={tvdbId}: status={add.status_code} body={add.text}")
        return False


def removeFromLibrary(tvdbId):
    parameters = {
        "deleteFiles": str(True)
    }
    try:
        dbId = getDbIdFromImdbId(tvdbId)
        delete = requests.delete(commons.generateApiQuery("sonarr", f"series/{dbId}", parameters))
    except Exception as e:
        logger.warning(f"Sonarr removeFromLibrary failed for tvdbId={tvdbId}: {e}")
        return False
    if delete.status_code == 200:
        return True
    else:
        logger.warning(f"Sonarr removeFromLibrary rejected tvdbId={tvdbId}: status={delete.status_code}")
        return False


def buildData(json, path, qualityProfileId, tags, seasonsSelected):
    built_data = {
        "qualityProfileId": qualityProfileId,
        "addOptions": {
            "ignoreEpisodesWithFiles": True,
            "ignoreEpisodesWithoutFiles": False,
            "searchForMissingEpisodes": config["search"],
        },
        "rootFolderPath": path,
        "seasonFolder": config["seasonFolder"],
        "monitored": True,
        "tags": tags,
        "seasons": seasonsSelected,
    }
    for show in json:
        for key, value in show.items():
            if key in addSerieNeededFields:
                built_data[key] = value
            if key == "seasons": built_data["seasons"] = seasonsSelected
    logger.debug(f"Query endpoint is: {commons.generateApiQuery('sonarr', 'series')}")
    return built_data


def getRootFolders():
    parameters = {}
    try:
        req = requests.get(commons.generateApiQuery("sonarr", "Rootfolder", parameters))
        parsed_json = json.loads(req.text)
    except Exception as e:
        logger.warning(f"Sonarr getRootFolders failed: {e}")
        return []
    # Remove unmappedFolders from rootFolder data--we don't need that
    for item in [
        item for item in parsed_json if item.get("unmappedFolders") is not None
    ]:
        item.pop("unmappedFolders")
    return parsed_json


def allSeries():
    parameters = {}
    try:
        req = requests.get(commons.generateApiQuery("sonarr", "series", parameters))
        parsed_json = json.loads(req.text)
    except Exception as e:
        logger.warning(f"Sonarr allSeries failed: {e}")
        return False

    if req.status_code == 200:
        data = []
        for show in parsed_json:
            if all(
                x in show
                for x in ["title", "year", "monitored", "status"]
            ):
                data.append(
                    {
                        "title": show["title"],
                        "year": show["year"],
                        "monitored": show["monitored"],
                        "status": show["status"],
                    }
                )
        return data
    else:
        logger.warning(f"Sonarr allSeries returned status={req.status_code}")
        return False


def getQualityProfiles():
    parameters = {}
    try:
        req = requests.get(commons.generateApiQuery("sonarr", "qualityProfile", parameters))
        return json.loads(req.text)
    except Exception as e:
        logger.warning(f"Sonarr getQualityProfiles failed: {e}")
        return []


def getTags():
    parameters = {}
    try:
        req = requests.get(commons.generateApiQuery("sonarr", "tag", parameters))
        return json.loads(req.text)
    except Exception as e:
        logger.warning(f"Sonarr getTags failed: {e}")
        return []


def createTag(tag):
    try:
        data_json = {
            "id": int(max([t["id"] for t in getTags()], default=0)+1),
            "label": str(tag)
        }
        add = requests.post(commons.generateApiQuery("sonarr", "tag"), json=data_json, headers={'Content-Type': 'application/json'})
    except Exception as e:
        logger.warning(f"Sonarr createTag failed for {tag!r}: {e}")
        return False
    if add.status_code == 200:
        return True
    else:
        logger.warning(f"Sonarr createTag rejected {tag!r}: status={add.status_code}")
        return False

def getSeasons(tvdbId):
    parameters = {"term": "tvdb:" + str(tvdbId)}
    try:
        req = requests.get(commons.generateApiQuery("sonarr", "series/lookup", parameters))
        parsed_json = json.loads(req.text)
        return parsed_json[0]["seasons"]
    except Exception as e:
        logger.warning(f"Sonarr getSeasons failed for tvdbId={tvdbId}: {e}")
        return []


def getDbIdFromImdbId(tvdbId):
    req = requests.get(commons.generateApiQuery("sonarr", "series", {}))
    parsed_json = json.loads(req.text)
    dbId = [f["id"] for f in parsed_json if f["tvdbId"] == tvdbId]
    return dbId[0]


def getQueue():
    """Active download queue with byte-level progress. Normalized dicts."""
    try:
        req = requests.get(commons.generateApiQuery("sonarr", "queue", {"pageSize": "50"}))
        records = json.loads(req.text).get("records", [])
    except Exception as e:
        logger.warning(f"Sonarr getQueue failed: {e}")
        return []
    out = []
    for r in records:
        size = r.get("size") or 0
        sizeleft = r.get("sizeleft") or 0
        out.append({
            "title": r.get("title", ""),
            "mediaType": "tv",
            "progress": round((size - sizeleft) / size * 100) if size else 0,
            "timeleft": r.get("timeleft", ""),
            "status": r.get("status", ""),
        })
    return out
