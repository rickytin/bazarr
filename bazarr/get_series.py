# coding=utf-8

import os
import requests
import logging
from queueconfig import notifications
from collections import OrderedDict
import datetime

from get_args import args
from config import settings, url_sonarr
from list_subtitles import list_missing_subtitles
from database import TableShows
from utils import get_sonarr_version


def update_series():
    notifications.write(msg="Update series list from Sonarr is running...", queue='get_series')
    apikey_sonarr = settings.sonarr.apikey
    sonarr_version = get_sonarr_version()
    serie_default_enabled = settings.general.getboolean('serie_default_enabled')
    serie_default_language = settings.general.serie_default_language
    serie_default_hi = settings.general.serie_default_hi
    serie_default_forced = settings.general.serie_default_forced
    
    if apikey_sonarr is None:
        pass
    else:
        audio_profiles = get_profile_list()
        
        # Get shows data from Sonarr
        url_sonarr_api_series = url_sonarr + "/api/series?apikey=" + apikey_sonarr
        try:
            r = requests.get(url_sonarr_api_series, timeout=60, verify=False)
            r.raise_for_status()
        except requests.exceptions.HTTPError as errh:
            logging.exception("BAZARR Error trying to get series from Sonarr. Http error.")
            return
        except requests.exceptions.ConnectionError as errc:
            logging.exception("BAZARR Error trying to get series from Sonarr. Connection Error.")
            return
        except requests.exceptions.Timeout as errt:
            logging.exception("BAZARR Error trying to get series from Sonarr. Timeout Error.")
            return
        except requests.exceptions.RequestException as err:
            logging.exception("BAZARR Error trying to get series from Sonarr.")
            return
        else:
            # Get current shows in DB
            current_shows_db = TableShows.select(
                TableShows.tvdb_id
            )
            
            current_shows_db_list = [x.tvdb_id for x in current_shows_db]
            current_shows_sonarr = []
            series_to_update = []
            series_to_add = []
            altered_series = []

            seriesListLength = len(r.json())
            for i, show in enumerate(r.json(), 1):
                notifications.write(msg="Getting series data from Sonarr...", queue='get_series', item=i, length=seriesListLength)
                try:
                    overview = unicode(show['overview'])
                except:
                    overview = ""
                try:
                    poster_big = show['images'][2]['url'].split('?')[0]
                    poster = os.path.splitext(poster_big)[0] + '-250' + os.path.splitext(poster_big)[1]
                except:
                    poster = ""
                try:
                    fanart = show['images'][0]['url'].split('?')[0]
                except:
                    fanart = ""

                if show['alternateTitles'] != None:
                    alternateTitles = str([item['title'] for item in show['alternateTitles']])
                else:
                    alternateTitles = None

                # Add shows in Sonarr to current shows list
                current_shows_sonarr.append(show['tvdbId'])
                
                if show['tvdbId'] in current_shows_db_list:
                    series_to_update.append({'title': unicode(show["title"]),
                                             'path': unicode(show["path"]),
                                             'tvdb_id': int(show["tvdbId"]),
                                             'sonarr_series_id': int(show["id"]),
                                             'overview': unicode(overview),
                                             'poster': unicode(poster),
                                             'fanart': unicode(fanart),
                                             'audio_language': unicode(profile_id_to_language((show['qualityProfileId'] if sonarr_version.startswith('2') else show['languageProfileId']), audio_profiles)),
                                             'sort_title': unicode(show['sortTitle']),
                                             'year': unicode(show['year']),
                                             'alternate_titles': unicode(alternateTitles)})
                else:
                    if serie_default_enabled is True:
                        series_to_add.append({'title': show["title"],
                                              'path': show["path"],
                                              'tvdb_id': show["tvdbId"],
                                              'languages': serie_default_language,
                                              'hearing_impaired': serie_default_hi,
                                              'sonarr_series_id': show["id"],
                                              'overview': overview,
                                              'poster': poster,
                                              'fanart': fanart,
                                              'audio_language': profile_id_to_language((show['qualityProfileId'] if sonarr_version.startswith('2') else show['languageProfileId']), audio_profiles),
                                              'sort_title': show['sortTitle'],
                                              'year': show['year'],
                                              'alternate_titles': alternateTitles,
                                              'forced': serie_default_forced})
                    else:
                        series_to_add.append({'title': show["title"],
                                              'path': show["path"],
                                              'tvdb_id': show["tvdbId"],
                                              'sonarr_series_id': show["id"],
                                              'overview': overview,
                                              'poster': poster,
                                              'fanart': fanart,
                                              'audio_language': profile_id_to_language((show['qualityProfileId'] if sonarr_version.startswith('2') else show['languageProfileId']), audio_profiles),
                                              'sort_title': show['sortTitle'],
                                              'year': show['year'],
                                              'alternate_titles': alternateTitles})
            
            # Update existing series in DB
            series_in_db_list = []
            series_in_db = TableShows.select(
                TableShows.title,
                TableShows.path,
                TableShows.tvdb_id,
                TableShows.sonarr_series_id,
                TableShows.overview,
                TableShows.poster,
                TableShows.fanart,
                TableShows.audio_language,
                TableShows.sort_title,
                TableShows.year,
                TableShows.alternate_titles
            ).dicts()

            for item in series_in_db:
                series_in_db_list.append(item)

            series_to_update_list = [i for i in series_to_update if i not in series_in_db_list]

            for updated_series in series_to_update_list:
                TableShows.update(
                    updated_series
                ).where(
                    TableShows.sonarr_series_id == updated_series['sonarr_series_id']
                ).execute()

            # Insert new series in DB
            for added_series in series_to_add:
                TableShows.insert(
                    added_series
                ).on_conflict_ignore().execute()
                list_missing_subtitles(added_series['sonarr_series_id'])

            # Remove old series from DB
            removed_series = list(set(current_shows_db_list) - set(current_shows_sonarr))

            for series in removed_series:
                TableShows.delete().where(
                    TableShows.tvdb_id == series
                ).execute()

            logging.debug('BAZARR All series synced from Sonarr into database.')


def get_profile_list():
    apikey_sonarr = settings.sonarr.apikey
    sonarr_version = get_sonarr_version()
    profiles_list = []
    # Get profiles data from Sonarr

    if sonarr_version.startswith('2'):
        url_sonarr_api_series = url_sonarr + "/api/profile?apikey=" + apikey_sonarr
    elif sonarr_version.startswith('3'):
        url_sonarr_api_series = url_sonarr + "/api/v3/languageprofile?apikey=" + apikey_sonarr

    try:
        profiles_json = requests.get(url_sonarr_api_series, timeout=60, verify=False)
    except requests.exceptions.ConnectionError as errc:
        logging.exception("BAZARR Error trying to get profiles from Sonarr. Connection Error.")
    except requests.exceptions.Timeout as errt:
        logging.exception("BAZARR Error trying to get profiles from Sonarr. Timeout Error.")
    except requests.exceptions.RequestException as err:
        logging.exception("BAZARR Error trying to get profiles from Sonarr.")
    else:
        # Parsing data returned from Sonarr
        if sonarr_version.startswith('2'):
            for profile in profiles_json.json():
                profiles_list.append([profile['id'], profile['language'].capitalize()])
        elif sonarr_version.startswith('3'):
            for profile in profiles_json.json():
                profiles_list.append([profile['id'], profile['name'].capitalize()])

        return profiles_list

    return None


def profile_id_to_language(id, profiles):
    for profile in profiles:
        if id == profile[0]:
            return profile[1]
