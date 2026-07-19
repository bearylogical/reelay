#!/usr/bin/env python3

import json
import logging

import requests

from . import commons
from . import logger
from .config import config

# Set up logging
logLevel = logging.DEBUG if config.get("debugLogging", False) else logging.INFO
logger = logger.getLogger("reelay.radarr", logLevel, config.get("logToConsole", False))

config = config["radarr"]

addMovieNeededFields = ["tmdbId", "year", "title", "titleSlug", "images"]


def search(title):
    parameters = {"term": title}
    url = commons.generateApiQuery("radarr", "movie/lookup", parameters)
    logger.info(url)
    try:
        req = requests.get(url)
        parsed_json = json.loads(req.text)
    except Exception as e:
        logger.warning(f"Radarr search failed: {e}")
        return False

    if req.status_code == 200 and parsed_json:
        return parsed_json
    else:
        logger.warning(f"Radarr search returned status={req.status_code} for {title!r}")
        return False


def giveTitles(parsed_json):
    data = []
    for movie in parsed_json:
        if all(
            x in movie for x in ["title", "overview", "year", "tmdbId"]
        ):
            data.append(
                {
                    "title": movie["title"],
                    "overview": movie["overview"],
                    "poster": movie.get("remotePoster", None),
                    "year": movie["year"],
                    "id": movie["tmdbId"],
                }
            )
    return data


def inLibrary(tmdbId):
    parameters = {}
    try:
        req = requests.get(commons.generateApiQuery("radarr", "movie", parameters))
        parsed_json = json.loads(req.text)
    except Exception as e:
        logger.warning(f"Radarr inLibrary check failed: {e}")
        return False
    return next((True for movie in parsed_json if movie["tmdbId"] == tmdbId), False)


def addToLibrary(tmdbId, path, qualityProfileId, tags):
    parameters = {"tmdbId": str(tmdbId)}
    try:
        req = requests.get(
            commons.generateApiQuery("radarr", "movie/lookup/tmdb", parameters)
        )
        parsed_json = json.loads(req.text)
        data = json.dumps(buildData(parsed_json, path, qualityProfileId, tags))
        add = requests.post(commons.generateApiQuery("radarr", "movie"), data=data, headers={'Content-Type': 'application/json'})
    except Exception as e:
        logger.warning(f"Radarr addToLibrary failed for tmdbId={tmdbId}: {e}")
        return False
    if add.status_code == 201:
        return True
    else:
        logger.warning(f"Radarr addToLibrary rejected tmdbId={tmdbId}: status={add.status_code} body={add.text}")
        return False


def removeFromLibrary(tmdbId):
    parameters = {
        "deleteFiles": str(True)
    }
    try:
        dbId = getDbIdFromImdbId(tmdbId)
        delete = requests.delete(commons.generateApiQuery("radarr", f"movie/{dbId}", parameters))
    except Exception as e:
        logger.warning(f"Radarr removeFromLibrary failed for tmdbId={tmdbId}: {e}")
        return False
    if delete.status_code == 200:
        return True
    else:
        logger.warning(f"Radarr removeFromLibrary rejected tmdbId={tmdbId}: status={delete.status_code}")
        return False


def buildData(json, path, qualityProfileId, tags):
    built_data = {
        "qualityProfileId": int(qualityProfileId),
        "minimumAvailability": config["minimumAvailability"],
        "rootFolderPath": path,
        "addOptions": {"searchForMovie": config["search"]},
        "tags": tags,
    }

    for key in addMovieNeededFields:
        built_data[key] = json[key]
    return built_data


def getRootFolders():
    parameters = {}
    try:
        req = requests.get(commons.generateApiQuery("radarr", "Rootfolder", parameters))
        return json.loads(req.text)
    except Exception as e:
        logger.warning(f"Radarr getRootFolders failed: {e}")
        return []


def all_movies():
    parameters = {}
    try:
        req = requests.get(commons.generateApiQuery("radarr", "movie", parameters))
        parsed_json = json.loads(req.text)
    except Exception as e:
        logger.warning(f"Radarr all_movies failed: {e}")
        return False

    if req.status_code == 200:
        data = []
        for movie in parsed_json:
            if all(
                x in movie
                for x in ["title", "year", "monitored", "status"]
            ):
                data.append(
                    {
                        "title": movie["title"],
                        "year": movie["year"],
                        "monitored": movie["monitored"],
                        "status": movie["status"]
                    }
                )
        return data
    else:
        logger.warning(f"Radarr all_movies returned status={req.status_code}")
        return False


def getQualityProfiles():
    parameters = {}
    try:
        req = requests.get(commons.generateApiQuery("radarr", "qualityProfile", parameters))
        return json.loads(req.text)
    except Exception as e:
        logger.warning(f"Radarr getQualityProfiles failed: {e}")
        return []


def getTags():
    parameters = {}
    try:
        req = requests.get(commons.generateApiQuery("radarr", "tag", parameters))
        return json.loads(req.text)
    except Exception as e:
        logger.warning(f"Radarr getTags failed: {e}")
        return []


def createTag(tag):
    try:
        data_json = {
            "id": max([t["id"] for t in getTags()], default=0)+1,
            "label": str(tag)
        }
        add = requests.post(commons.generateApiQuery("radarr", "tag"), json=data_json, headers={'Content-Type': 'application/json'})
    except Exception as e:
        logger.warning(f"Radarr createTag failed for {tag!r}: {e}")
        return False
    if add.status_code == 200:
        return True
    else:
        logger.warning(f"Radarr createTag rejected {tag!r}: status={add.status_code}")
        return False


def getDbIdFromImdbId(tmdbId):
    req = requests.get(commons.generateApiQuery("radarr", "movie", {}))
    parsed_json = json.loads(req.text)
    dbId = [f["id"] for f in parsed_json if f["tmdbId"] == tmdbId]
    return dbId[0]


def getQueue():
    """Active download queue with byte-level progress. Normalized dicts."""
    try:
        req = requests.get(commons.generateApiQuery("radarr", "queue", {"pageSize": "50"}))
        records = json.loads(req.text).get("records", [])
    except Exception as e:
        logger.warning(f"Radarr getQueue failed: {e}")
        return []
    return [_normalizeQueueRecord(r, "movie") for r in records]


def _normalizeQueueRecord(r, media_type):
    size = r.get("size") or 0
    sizeleft = r.get("sizeleft") or 0
    progress = round((size - sizeleft) / size * 100) if size else 0
    return {
        "title": r.get("title", ""),
        "mediaType": media_type,
        "progress": progress,
        "timeleft": r.get("timeleft", ""),
        "status": r.get("status", ""),
    }