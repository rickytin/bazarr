# coding=utf-8

bazarr_version = '0.8.2.2'

import gc
import sys
import libs
import bottle
import itertools
import operator
import pretty
import math
import ast
import hashlib
import urllib
import warnings
import queueconfig
import platform
import apprise
from peewee import fn, JOIN
import operator
from calendar import day_name

from get_args import args
from init import *
from database import database, database_init, TableEpisodes, TableShows, TableMovies, TableHistory, TableHistoryMovie, \
    TableSettingsLanguages, TableSettingsNotifier, System

# Initiate database
database_init()

from notifier import update_notifier
from logger import configure_logging, empty_log

from cherrypy.wsgiserver import CherryPyWSGIServer

from io import BytesIO
from six import text_type
from beaker.middleware import SessionMiddleware
from cork import Cork
from bottle import route, template, static_file, request, redirect, response, HTTPError, app, hook
from datetime import timedelta
from get_languages import load_language_in_db, language_from_alpha3
from get_providers import get_providers, get_providers_auth, list_throttled_providers
from get_series import *
from get_episodes import *
from get_movies import *

from list_subtitles import store_subtitles, store_subtitles_movie, series_scan_subtitles, movies_scan_subtitles, \
    list_missing_subtitles, list_missing_subtitles_movies
from get_subtitle import download_subtitle, series_download_subtitles, movies_download_subtitles, \
    manual_search, manual_download_subtitle, manual_upload_subtitle
from utils import history_log, history_log_movie, get_sonarr_version, get_radarr_version
from scheduler import *
from notifier import send_notifications, send_notifications_movie
from config import settings, url_sonarr, url_radarr, url_radarr_short, url_sonarr_short, base_url
from subliminal_patch.extensions import provider_registry as provider_manager
from subliminal_patch.core import SUBTITLE_EXTENSIONS

reload(sys)
sys.setdefaultencoding('utf8')
gc.enable()

os.environ["SZ_USER_AGENT"] = "Bazarr/1"
os.environ["BAZARR_VERSION"] = bazarr_version

configure_logging(settings.general.getboolean('debug') or args.debug)

# Check and install update on startup when running on Windows from installer
if args.release_update:
    check_and_apply_update()

if settings.proxy.type != 'None':
    if settings.proxy.username != '' and settings.proxy.password != '':
        proxy = settings.proxy.type + '://' + settings.proxy.username + ':' + settings.proxy.password + '@' + \
                settings.proxy.url + ':' + settings.proxy.port
    else:
        proxy = settings.proxy.type + '://' + settings.proxy.url + ':' + settings.proxy.port
    os.environ['HTTP_PROXY'] = str(proxy)
    os.environ['HTTPS_PROXY'] = str(proxy)
    os.environ['NO_PROXY'] = str(settings.proxy.exclude)

bottle.TEMPLATE_PATH.insert(0, os.path.join(os.path.dirname(__file__), '../views/'))
if "PYCHARM_HOSTED" in os.environ:
    bottle.debug(True)
    bottle.TEMPLATES.clear()
else:
    bottle.ERROR_PAGE_TEMPLATE = bottle.ERROR_PAGE_TEMPLATE.replace('if DEBUG and', 'if')

# Reset restart required warning on start
if System.select().count():
    System.update({
        System.configured: 0,
        System.updated: 0
    }).execute()
else:
    System.insert({
        System.configured: 0,
        System.updated: 0
    }).execute()

# Load languages in database
load_language_in_db()

aaa = Cork(os.path.normpath(os.path.join(args.config_dir, 'config')))

app = app()
session_opts = {
    'session.cookie_expires': True,
    'session.key': 'Bazarr',
    'session.httponly': True,
    'session.timeout': 3600 * 24,  # 1 day
    'session.type': 'cookie',
    'session.validate_key': True
}
app = SessionMiddleware(app, session_opts)
login_auth = settings.auth.type


update_notifier()


def custom_auth_basic(check):
    def decorator(func):
        def wrapper(*a, **ka):
            if settings.auth.type == 'basic':
                user, password = request.auth or (None, None)
                if user is None or not check(user, password):
                    err = HTTPError(401, "Access denied")
                    err.add_header('WWW-Authenticate', 'Basic realm="Bazarr"')
                    return err
                return func(*a, **ka)
            else:
                return func(*a, **ka)
        
        return wrapper
    
    return decorator


def check_credentials(user, pw):
    username = settings.auth.username
    password = settings.auth.password
    if hashlib.md5(pw).hexdigest() == password and user == username:
        return True
    return False


def authorize():
    if login_auth == 'form':
        aaa = Cork(os.path.normpath(os.path.join(args.config_dir, 'config')))
        aaa.require(fail_redirect=(base_url + 'login'))


def post_get(name, default=''):
    return request.POST.get(name, default).strip()


@hook('before_request')
def enable_cors():
    response.headers['Access-Control-Allow-Origin'] = '*'


@route(base_url + 'login')
def login_form():
    msg = bottle.request.query.get('msg', '')
    return template('login', base_url=base_url, msg=msg)


@route(base_url + 'login', method='POST')
def login():
    aaa = Cork(os.path.normpath(os.path.join(args.config_dir, 'config')))
    username = post_get('username')
    password = post_get('password')
    aaa.login(username, password, success_redirect=base_url, fail_redirect=(base_url + 'login?msg=fail'))


@route(base_url + 'logout')
def logout():
    aaa.logout(success_redirect=(base_url + 'login'))


@route('/')
@custom_auth_basic(check_credentials)
def redirect_root():
    authorize()
    redirect(base_url)


@route(base_url + 'shutdown')
def shutdown():
    try:
        stop_file = open(os.path.join(args.config_dir, "bazarr.stop"), "w")
    except Exception as e:
        logging.error('BAZARR Cannot create bazarr.stop file.')
    else:
        server.stop()
        database.close()
        database.stop()
        stop_file.write('')
        stop_file.close()
        sys.exit(0)


@route(base_url + 'restart')
def restart():
    try:
        restart_file = open(os.path.join(args.config_dir, "bazarr.restart"), "w")
    except Exception as e:
        logging.error('BAZARR Cannot create bazarr.restart file.')
    else:
        # print 'Bazarr is being restarted...'
        logging.info('Bazarr is being restarted...')
        server.stop()
        database.close()
        database.stop()
        restart_file.write('')
        restart_file.close()
        sys.exit(0)


@route(base_url + 'wizard')
@custom_auth_basic(check_credentials)
def wizard():
    authorize()

    # Get languages list
    settings_languages = TableSettingsLanguages.select().order_by(TableSettingsLanguages.name)
    # Get providers list
    settings_providers = sorted(provider_manager.names())

    return template('wizard', bazarr_version=bazarr_version, settings=settings,
                    settings_languages=settings_languages, settings_providers=settings_providers,
                    base_url=base_url)


@route(base_url + 'save_wizard', method='POST')
@custom_auth_basic(check_credentials)
def save_wizard():
    authorize()

    settings_general_ip = request.forms.get('settings_general_ip')
    settings_general_port = request.forms.get('settings_general_port')
    settings_general_baseurl = request.forms.get('settings_general_baseurl')
    if not settings_general_baseurl.endswith('/'):
        settings_general_baseurl += '/'
    settings_general_sourcepath = request.forms.getall('settings_general_sourcepath')
    settings_general_destpath = request.forms.getall('settings_general_destpath')
    settings_general_pathmapping = []
    settings_general_pathmapping.extend([list(a) for a in zip(settings_general_sourcepath, settings_general_destpath)])
    settings_general_sourcepath_movie = request.forms.getall('settings_general_sourcepath_movie')
    settings_general_destpath_movie = request.forms.getall('settings_general_destpath_movie')
    settings_general_pathmapping_movie = []
    settings_general_pathmapping_movie.extend(
        [list(a) for a in zip(settings_general_sourcepath_movie, settings_general_destpath_movie)])
    settings_general_single_language = request.forms.get('settings_general_single_language')
    if settings_general_single_language is None:
        settings_general_single_language = 'False'
    else:
        settings_general_single_language = 'True'
    settings_general_use_sonarr = request.forms.get('settings_general_use_sonarr')
    if settings_general_use_sonarr is None:
        settings_general_use_sonarr = 'False'
    else:
        settings_general_use_sonarr = 'True'
    settings_general_use_radarr = request.forms.get('settings_general_use_radarr')
    if settings_general_use_radarr is None:
        settings_general_use_radarr = 'False'
    else:
        settings_general_use_radarr = 'True'
    settings_general_embedded = request.forms.get('settings_general_embedded')
    if settings_general_embedded is None:
        settings_general_embedded = 'False'
    else:
        settings_general_embedded = 'True'
    settings_subfolder = request.forms.get('settings_subfolder')
    settings_subfolder_custom = request.forms.get('settings_subfolder_custom')
    settings_upgrade_subs = request.forms.get('settings_upgrade_subs')
    if settings_upgrade_subs is None:
        settings_upgrade_subs = 'False'
    else:
        settings_upgrade_subs = 'True'
    settings_days_to_upgrade_subs = request.forms.get('settings_days_to_upgrade_subs')
    settings_upgrade_manual = request.forms.get('settings_upgrade_manual')
    if settings_upgrade_manual is None:
        settings_upgrade_manual = 'False'
    else:
        settings_upgrade_manual = 'True'
    
    settings.general.ip = text_type(settings_general_ip)
    settings.general.port = text_type(settings_general_port)
    settings.general.base_url = text_type(settings_general_baseurl)
    settings.general.path_mappings = text_type(settings_general_pathmapping)
    settings.general.single_language = text_type(settings_general_single_language)
    settings.general.use_sonarr = text_type(settings_general_use_sonarr)
    settings.general.use_radarr = text_type(settings_general_use_radarr)
    settings.general.path_mappings_movie = text_type(settings_general_pathmapping_movie)
    settings.general.subfolder = text_type(settings_subfolder)
    settings.general.subfolder_custom = text_type(settings_subfolder_custom)
    settings.general.use_embedded_subs = text_type(settings_general_embedded)
    settings.general.upgrade_subs = text_type(settings_upgrade_subs)
    settings.general.days_to_upgrade_subs = text_type(settings_days_to_upgrade_subs)
    settings.general.upgrade_manual = text_type(settings_upgrade_manual)
    
    settings_sonarr_ip = request.forms.get('settings_sonarr_ip')
    settings_sonarr_port = request.forms.get('settings_sonarr_port')
    settings_sonarr_baseurl = request.forms.get('settings_sonarr_baseurl')
    settings_sonarr_ssl = request.forms.get('settings_sonarr_ssl')
    if settings_sonarr_ssl is None:
        settings_sonarr_ssl = 'False'
    else:
        settings_sonarr_ssl = 'True'
    settings_sonarr_apikey = request.forms.get('settings_sonarr_apikey')
    settings_sonarr_only_monitored = request.forms.get('settings_sonarr_only_monitored')
    if settings_sonarr_only_monitored is None:
        settings_sonarr_only_monitored = 'False'
    else:
        settings_sonarr_only_monitored = 'True'
    
    settings.sonarr.ip = text_type(settings_sonarr_ip)
    settings.sonarr.port = text_type(settings_sonarr_port)
    settings.sonarr.base_url = text_type(settings_sonarr_baseurl)
    settings.sonarr.ssl = text_type(settings_sonarr_ssl)
    settings.sonarr.apikey = text_type(settings_sonarr_apikey)
    settings.sonarr.only_monitored = text_type(settings_sonarr_only_monitored)
    
    settings_radarr_ip = request.forms.get('settings_radarr_ip')
    settings_radarr_port = request.forms.get('settings_radarr_port')
    settings_radarr_baseurl = request.forms.get('settings_radarr_baseurl')
    settings_radarr_ssl = request.forms.get('settings_radarr_ssl')
    if settings_radarr_ssl is None:
        settings_radarr_ssl = 'False'
    else:
        settings_radarr_ssl = 'True'
    settings_radarr_apikey = request.forms.get('settings_radarr_apikey')
    settings_radarr_only_monitored = request.forms.get('settings_radarr_only_monitored')
    if settings_radarr_only_monitored is None:
        settings_radarr_only_monitored = 'False'
    else:
        settings_radarr_only_monitored = 'True'
    
    settings.radarr.ip = text_type(settings_radarr_ip)
    settings.radarr.port = text_type(settings_radarr_port)
    settings.radarr.base_url = text_type(settings_radarr_baseurl)
    settings.radarr.ssl = text_type(settings_radarr_ssl)
    settings.radarr.apikey = text_type(settings_radarr_apikey)
    settings.radarr.only_monitored = text_type(settings_radarr_only_monitored)
    
    settings_subliminal_providers = request.forms.getall('settings_subliminal_providers')
    settings.general.enabled_providers = u'' if not settings_subliminal_providers else ','.join(
        settings_subliminal_providers)
    
    settings_addic7ed_random_agents = request.forms.get('settings_addic7ed_random_agents')
    if settings_addic7ed_random_agents is None:
        settings_addic7ed_random_agents = 'False'
    else:
        settings_addic7ed_random_agents = 'True'
    
    settings_opensubtitles_vip = request.forms.get('settings_opensubtitles_vip')
    if settings_opensubtitles_vip is None:
        settings_opensubtitles_vip = 'False'
    else:
        settings_opensubtitles_vip = 'True'
    
    settings_opensubtitles_ssl = request.forms.get('settings_opensubtitles_ssl')
    if settings_opensubtitles_ssl is None:
        settings_opensubtitles_ssl = 'False'
    else:
        settings_opensubtitles_ssl = 'True'
    
    settings_opensubtitles_skip_wrong_fps = request.forms.get('settings_opensubtitles_skip_wrong_fps')
    if settings_opensubtitles_skip_wrong_fps is None:
        settings_opensubtitles_skip_wrong_fps = 'False'
    else:
        settings_opensubtitles_skip_wrong_fps = 'True'
    
    settings.addic7ed.username = request.forms.get('settings_addic7ed_username')
    settings.addic7ed.password = request.forms.get('settings_addic7ed_password')
    settings.addic7ed.random_agents = text_type(settings_addic7ed_random_agents)
    settings.assrt.token = request.forms.get('settings_assrt_token')
    settings.legendastv.username = request.forms.get('settings_legendastv_username')
    settings.legendastv.password = request.forms.get('settings_legendastv_password')
    settings.opensubtitles.username = request.forms.get('settings_opensubtitles_username')
    settings.opensubtitles.password = request.forms.get('settings_opensubtitles_password')
    settings.opensubtitles.vip = text_type(settings_opensubtitles_vip)
    settings.opensubtitles.ssl = text_type(settings_opensubtitles_ssl)
    settings.opensubtitles.skip_wrong_fps = text_type(settings_opensubtitles_skip_wrong_fps)
    settings.xsubs.username = request.forms.get('settings_xsubs_username')
    settings.xsubs.password = request.forms.get('settings_xsubs_password')
    settings.napisy24.username = request.forms.get('settings_napisy24_username')
    settings.napisy24.password = request.forms.get('settings_napisy24_password')
    settings.subscene.username = request.forms.get('settings_subscene_username')
    settings.subscene.password = request.forms.get('settings_subscene_password')
    settings.titlovi.username = request.forms.get('settings_titlovi_username')
    settings.titlovi.password = request.forms.get('settings_titlovi_password')
    settings.betaseries.token = request.forms.get('settings_betaseries_token')
    
    settings_subliminal_languages = request.forms.getall('settings_subliminal_languages')
    # Disable all languages in DB
    TableSettingsLanguages.update({TableSettingsLanguages.enabled: 0})
    for item in settings_subliminal_languages:
        # Enable each desired language in DB
        TableSettingsLanguages.update(
            {TableSettingsLanguages.enabled: 1}
        ).where(
            TableSettingsLanguages.code2 == item
        ).execute()
    
    settings_serie_default_enabled = request.forms.get('settings_serie_default_enabled')
    if settings_serie_default_enabled is None:
        settings_serie_default_enabled = 'False'
    else:
        settings_serie_default_enabled = 'True'
    settings.general.serie_default_enabled = text_type(settings_serie_default_enabled)
    
    settings_serie_default_languages = str(request.forms.getall('settings_serie_default_languages'))
    if settings_serie_default_languages == "['None']":
        settings_serie_default_languages = 'None'
    settings.general.serie_default_language = text_type(settings_serie_default_languages)
    
    settings_serie_default_hi = request.forms.get('settings_serie_default_hi')
    if settings_serie_default_hi is None:
        settings_serie_default_hi = 'False'
    else:
        settings_serie_default_hi = 'True'
    settings.general.serie_default_hi = text_type(settings_serie_default_hi)
    
    settings_movie_default_enabled = request.forms.get('settings_movie_default_enabled')
    if settings_movie_default_enabled is None:
        settings_movie_default_enabled = 'False'
    else:
        settings_movie_default_enabled = 'True'
    settings.general.movie_default_enabled = text_type(settings_movie_default_enabled)
    
    settings_movie_default_languages = str(request.forms.getall('settings_movie_default_languages'))
    if settings_movie_default_languages == "['None']":
        settings_movie_default_languages = 'None'
    settings.general.movie_default_language = text_type(settings_movie_default_languages)
    
    settings_movie_default_hi = request.forms.get('settings_movie_default_hi')
    if settings_movie_default_hi is None:
        settings_movie_default_hi = 'False'
    else:
        settings_movie_default_hi = 'True'
    settings.general.movie_default_hi = text_type(settings_movie_default_hi)
    
    settings_movie_default_forced = str(request.forms.get('settings_movie_default_forced'))
    settings.general.movie_default_forced = text_type(settings_movie_default_forced)
    
    with open(os.path.join(args.config_dir, 'config', 'config.ini'), 'w+') as handle:
        settings.write(handle)

    configured()
    redirect(base_url)


@route(base_url + 'static/:path#.+#', name='static')
@custom_auth_basic(check_credentials)
def static(path):
    return static_file(path, root=os.path.join(os.path.dirname(__file__), '../static'))


@route(base_url + 'emptylog')
@custom_auth_basic(check_credentials)
def emptylog():
    authorize()
    ref = request.environ['HTTP_REFERER']
    
    empty_log()
    logging.info('BAZARR Log file emptied')
    
    redirect(ref)


@route(base_url + 'bazarr.log')
@custom_auth_basic(check_credentials)
def download_log():
    authorize()
    return static_file('bazarr.log', root=os.path.join(args.config_dir, 'log/'), download='bazarr.log')


@route(base_url + 'image_proxy/<url:path>', method='GET')
@custom_auth_basic(check_credentials)
def image_proxy(url):
    authorize()
    apikey = settings.sonarr.apikey
    url_image = url_sonarr_short + '/' + url + '?apikey=' + apikey
    try:
        image_buffer = BytesIO(
            requests.get(url_sonarr + '/api' + url_image.split(url_sonarr)[1], timeout=15, verify=False).content)
    except:
        return None
    else:
        image_buffer.seek(0)
        bytes = image_buffer.read()
        response.set_header('Content-type', 'image/jpeg')
        return bytes


@route(base_url + 'image_proxy_movies/<url:path>', method='GET')
@custom_auth_basic(check_credentials)
def image_proxy_movies(url):
    authorize()
    apikey = settings.radarr.apikey
    try:
        url_image = (url_radarr_short + '/' + url + '?apikey=' + apikey).replace('/fanart.jpg', '/banner.jpg')
        image_buffer = BytesIO(
            requests.get(url_radarr + '/api' + url_image.split(url_radarr)[1], timeout=15, verify=False).content)
    except:
        url_image = url_radarr_short + '/' + url + '?apikey=' + apikey
        image_buffer = BytesIO(
            requests.get(url_radarr + '/api' + url_image.split(url_radarr)[1], timeout=15, verify=False).content)
    else:
        image_buffer.seek(0)
        bytes = image_buffer.read()
        response.set_header('Content-type', 'image/jpeg')
        return bytes


@route(base_url)
@route(base_url.rstrip('/'))
@custom_auth_basic(check_credentials)
def redirect_root():
    authorize()
    if settings.general.getboolean('use_sonarr'):
        redirect(base_url + 'series')
    elif settings.general.getboolean('use_radarr'):
        redirect(base_url + 'movies')
    elif not settings.general.enabled_providers:
        redirect(base_url + 'wizard')
    else:
        redirect(base_url + 'settings')


@route(base_url + 'series')
@custom_auth_basic(check_credentials)
def series():
    authorize()

    missing_count = TableShows.select().count()
    page = request.GET.page
    if page == "":
        page = "1"
    page_size = int(settings.general.page_size)
    offset = (int(page) - 1) * page_size
    max_page = int(math.ceil(missing_count / (page_size + 0.0)))
    
    # Get list of series
    data = TableShows.select(
        TableShows.tvdb_id,
        TableShows.title,
        fn.path_substitution(TableShows.path).alias('path'),
        TableShows.languages,
        TableShows.hearing_impaired,
        TableShows.sonarr_series_id,
        TableShows.poster,
        TableShows.audio_language,
        TableShows.forced
    ).order_by(
        TableShows.sort_title.asc()
    ).paginate(
        int(page),
        page_size
    )

    # Get languages list
    languages = TableSettingsLanguages.select(
        TableSettingsLanguages.code2,
        TableSettingsLanguages.name
    ).where(
        TableSettingsLanguages.enabled == 1
    )

    # Build missing subtitles clause depending on only_monitored
    missing_subtitles_clause = [
        (TableShows.languages != 'None'),
        (TableEpisodes.missing_subtitles != '[]')
    ]
    if settings.sonarr.getboolean('only_monitored'):
        missing_subtitles_clause.append(
            (TableEpisodes.monitored == 'True')
        )

    # Get missing subtitles count by series
    missing_subtitles_list = TableShows.select(
        TableShows.sonarr_series_id,
        fn.COUNT(TableEpisodes.missing_subtitles).alias('missing_subtitles')
    ).join_from(
        TableShows, TableEpisodes, JOIN.LEFT_OUTER
    ).where(
        reduce(operator.and_, missing_subtitles_clause)
    ).group_by(
        TableShows.sonarr_series_id
    )

    # Build total subtitles clause depending on only_monitored
    total_subtitles_clause = [
        (TableShows.languages != 'None')
    ]
    if settings.sonarr.getboolean('only_monitored'):
        total_subtitles_clause.append(
            (TableEpisodes.monitored == 'True')
        )

    # Get total subtitles count by series
    total_subtitles_list = TableShows.select(
        TableShows.sonarr_series_id,
        fn.COUNT(TableEpisodes.missing_subtitles).alias('missing_subtitles')
    ).join_from(
        TableShows, TableEpisodes, JOIN.LEFT_OUTER
    ).where(
        reduce(operator.and_, total_subtitles_clause)
    ).group_by(
        TableShows.sonarr_series_id
    )

    return template('series', bazarr_version=bazarr_version, rows=data,
                      missing_subtitles_list=missing_subtitles_list, total_subtitles_list=total_subtitles_list,
                      languages=languages, missing_count=missing_count, page=page, max_page=max_page, base_url=base_url,
                      single_language=settings.general.getboolean('single_language'), page_size=page_size,
                      current_port=settings.general.port)


@route(base_url + 'serieseditor')
@custom_auth_basic(check_credentials)
def serieseditor():
    authorize()
    
    # Get missing count
    missing_count = TableShows().select().count()
    
    # Get movies list
    data = TableShows.select(
        TableShows.tvdb_id,
        TableShows.title,
        fn.path_substitution(TableShows.path).alias('path'),
        TableShows.languages,
        TableShows.hearing_impaired,
        TableShows.sonarr_series_id,
        TableShows.poster,
        TableShows.audio_language,
        TableShows.forced
    ).order_by(
        TableShows.sort_title.asc()
    )

    # Get languages list
    languages = TableSettingsLanguages.select(
        TableSettingsLanguages.code2,
        TableSettingsLanguages.name
    ).where(
        TableSettingsLanguages.enabled == 1
    )

    return template('serieseditor', bazarr_version=bazarr_version, rows=data, languages=languages,
                    missing_count=missing_count, base_url=base_url,
                    single_language=settings.general.getboolean('single_language'), current_port=settings.general.port)


@route(base_url + 'search_json/<query>', method='GET')
@custom_auth_basic(check_credentials)
def search_json(query):
    authorize()

    query = '%' + query + '%'
    search_list = []

    if settings.general.getboolean('use_sonarr'):
        # Get matching series
        series = TableShows.select(
            TableShows.title,
            TableShows.sonarr_series_id,
            TableShows.year
        ).where(
            TableShows.title ** query
        ).order_by(
            TableShows.title.asc()
        )
        for serie in series:
            search_list.append(dict([('name', re.sub(r'\ \(\d{4}\)', '', serie.title) + ' (' + serie.year + ')'),
                                     ('url', base_url + 'episodes/' + str(serie.sonarr_series_id))]))
    
    if settings.general.getboolean('use_radarr'):
        # Get matching movies
        movies = TableMovies.select(
            TableMovies.title,
            TableMovies.radarr_id,
            TableMovies.year
        ).where(
            TableMovies.title ** query
        ).order_by(
            TableMovies.title.asc()
        )
        for movie in movies:
            search_list.append(dict([('name', re.sub(r'\ \(\d{4}\)', '', movie.title) + ' (' + movie.year + ')'),
                                     ('url', base_url + 'movie/' + str(movie.radarr_id))]))

    response.content_type = 'application/json'
    return dict(items=search_list)


@route(base_url + 'edit_series/<no:int>', method='POST')
@custom_auth_basic(check_credentials)
def edit_series(no):
    authorize()
    ref = request.environ['HTTP_REFERER']
    
    lang = request.forms.getall('languages')
    if len(lang) > 0:
        pass
    else:
        lang = 'None'
    
    single_language = settings.general.getboolean('single_language')
    if single_language:
        if str(lang) == "['None']":
            lang = 'None'
        else:
            lang = str(lang)
    else:
        if str(lang) == "['']":
            lang = '[]'
    
    hi = request.forms.get('hearing_impaired')
    forced = request.forms.get('forced')
    
    if hi == "on":
        hi = "True"
    else:
        hi = "False"

    result = TableShows.update(
        {
            TableShows.languages: lang,
            TableShows.hearing_impaired: hi,
            TableShows.forced: forced
        }
    ).where(
        TableShows.sonarr_series_id ** no
    ).execute()

    list_missing_subtitles(no)
    
    redirect(ref)


@route(base_url + 'edit_serieseditor', method='POST')
@custom_auth_basic(check_credentials)
def edit_serieseditor():
    authorize()
    ref = request.environ['HTTP_REFERER']
    
    series = request.forms.get('series')
    series = ast.literal_eval(str('[' + series + ']'))
    lang = request.forms.getall('languages')
    hi = request.forms.get('hearing_impaired')
    forced = request.forms.get('forced')

    for serie in series:
        if str(lang) != "[]" and str(lang) != "['']":
            if str(lang) == "['None']":
                lang = 'None'
            else:
                lang = str(lang)
            TableShows.update(
                {
                    TableShows.languages: lang
                }
            ).where(
                TableShows.sonarr_series_id % serie
            ).execute()
        if hi != '':
            TableShows.update(
                {
                    TableShows.hearing_impaired: hi
                }
            ).where(
                TableShows.sonarr_series_id % serie
            ).execute()
        if forced != '':
            TableShows.update(
                {
                    TableShows.forced: forced
                }
            ).where(
                TableShows.sonarr_series_id % serie
            ).execute()
    
    for serie in series:
        list_missing_subtitles(serie)
    
    redirect(ref)


@route(base_url + 'episodes/<no:int>', method='GET')
@custom_auth_basic(check_credentials)
def episodes(no):
    authorize()

    series_details = TableShows.select(
        TableShows.title,
        TableShows.overview,
        TableShows.poster,
        TableShows.fanart,
        TableShows.hearing_impaired,
        TableShows.tvdb_id,
        TableShows.audio_language,
        TableShows.languages,
        fn.path_substitution(TableShows.path).alias('path'),
        TableShows.forced
    ).where(
        TableShows.sonarr_series_id ** str(no)
    ).limit(1)
    for series in series_details:
        tvdbid = series.tvdb_id
        series_details = series
        break
    
    episodes = TableEpisodes.select(
        TableEpisodes.title,
        fn.path_substitution(TableEpisodes.path).alias('path'),
        TableEpisodes.season,
        TableEpisodes.episode,
        TableEpisodes.subtitles,
        TableEpisodes.sonarr_series_id,
        TableEpisodes.missing_subtitles,
        TableEpisodes.sonarr_episode_id,
        TableEpisodes.scene_name,
        TableEpisodes.monitored,
        TableEpisodes.failed_attempts
    ).where(
        TableEpisodes.sonarr_series_id % no
    ).order_by(
        TableEpisodes.season.desc(),
        TableEpisodes.episode.desc()
    )

    number = len(episodes)

    languages = TableSettingsLanguages.select(
        TableSettingsLanguages.code2,
        TableSettingsLanguages.name
    ).where(
        TableSettingsLanguages.enabled == 1
    )

    seasons_list = []
    for key, season in itertools.groupby(episodes.dicts(), lambda x: x['season']):
        seasons_list.append(list(season))

    return template('episodes', bazarr_version=bazarr_version, no=no, details=series_details,
                    languages=languages, seasons=seasons_list, url_sonarr_short=url_sonarr_short, base_url=base_url,
                    tvdbid=tvdbid, number=number, current_port=settings.general.port)


@route(base_url + 'movies')
@custom_auth_basic(check_credentials)
def movies():
    authorize()

    missing_count = TableMovies.select().count()
    page = request.GET.page
    if page == "":
        page = "1"
    page_size = int(settings.general.page_size)
    offset = (int(page) - 1) * page_size
    max_page = int(math.ceil(missing_count / (page_size + 0.0)))
    
    data = TableMovies.select(
        TableMovies.tmdb_id,
        TableMovies.title,
        fn.path_substitution_movie(TableMovies.path).alias('path'),
        TableMovies.languages,
        TableMovies.hearing_impaired,
        TableMovies.radarr_id,
        TableMovies.poster,
        TableMovies.audio_language,
        TableMovies.monitored,
        TableMovies.scene_name,
        TableMovies.forced
    ).order_by(
        TableMovies.sort_title.asc()
    ).paginate(
        int(page),
        page_size
    )

    languages = TableSettingsLanguages.select(
        TableSettingsLanguages.code2,
        TableSettingsLanguages.name
    ).where(
        TableSettingsLanguages.enabled == 1
    )

    output = template('movies', bazarr_version=bazarr_version, rows=data, languages=languages,
                      missing_count=missing_count, page=page, max_page=max_page, base_url=base_url,
                      single_language=settings.general.getboolean('single_language'), page_size=page_size,
                      current_port=settings.general.port)
    return output


@route(base_url + 'movieseditor')
@custom_auth_basic(check_credentials)
def movieseditor():
    authorize()
    
    missing_count = TableMovies.select().count()
    
    data = TableMovies.select(
        TableMovies.tmdb_id,
        TableMovies.title,
        fn.path_substitution_movie(TableMovies.path).alias('path'),
        TableMovies.languages,
        TableMovies.hearing_impaired,
        TableMovies.radarr_id,
        TableMovies.poster,
        TableMovies.audio_language,
        TableMovies.forced
    ).order_by(
        TableMovies.sort_title.asc()
    )

    languages = TableSettingsLanguages.select(
        TableSettingsLanguages.code2,
        TableSettingsLanguages.name
    ).where(
        TableSettingsLanguages.enabled == 1
    )

    output = template('movieseditor', bazarr_version=bazarr_version, rows=data, languages=languages,
                      missing_count=missing_count, base_url=base_url,
                      single_language=settings.general.getboolean('single_language'), current_port=settings.general.port)
    return output


@route(base_url + 'edit_movieseditor', method='POST')
@custom_auth_basic(check_credentials)
def edit_movieseditor():
    authorize()
    ref = request.environ['HTTP_REFERER']
    
    movies = request.forms.get('movies')
    movies = ast.literal_eval(str('[' + movies + ']'))
    lang = request.forms.getall('languages')
    hi = request.forms.get('hearing_impaired')
    forced = request.forms.get('forced')

    for movie in movies:
        if str(lang) != "[]" and str(lang) != "['']":
            if str(lang) == "['None']":
                lang = 'None'
            else:
                lang = str(lang)
            TableMovies.update(
                {
                    TableMovies.languages: lang
                }
            ).where(
                TableMovies.radarr_id % movie
            ).execute()
        if hi != '':
            TableMovies.update(
                {
                    TableMovies.hearing_impaired: hi
                }
            ).where(
                TableMovies.radarr_id % movie
            ).execute()
        if forced != '':
            TableMovies.update(
                {
                    TableMovies.forced: forced
                }
            ).where(
                TableMovies.radarr_id % movie
            ).execute()
    
    for movie in movies:
        list_missing_subtitles_movies(movie)
    
    redirect(ref)


@route(base_url + 'edit_movie/<no:int>', method='POST')
@custom_auth_basic(check_credentials)
def edit_movie(no):
    authorize()
    ref = request.environ['HTTP_REFERER']
    
    lang = request.forms.getall('languages')
    if len(lang) > 0:
        pass
    else:
        lang = 'None'
    
    single_language = settings.general.getboolean('single_language')
    if single_language:
        if str(lang) == "['None']":
            lang = 'None'
        else:
            lang = str(lang)
    else:
        if str(lang) == "['']":
            lang = '[]'
    
    hi = request.forms.get('hearing_impaired')
    forced = request.forms.get('forced')
    
    if hi == "on":
        hi = "True"
    else:
        hi = "False"
    
    TableMovies.update(
        {
            TableMovies.languages: str(lang),
            TableMovies.hearing_impaired: hi,
            TableMovies.forced: forced
        }
    ).where(
        TableMovies.radarr_id % no
    ).execute()

    list_missing_subtitles_movies(no)
    
    redirect(ref)


@route(base_url + 'movie/<no:int>', method='GET')
@custom_auth_basic(check_credentials)
def movie(no):
    authorize()
    
    movies_details = TableMovies.select(
        TableMovies.title,
        TableMovies.overview,
        TableMovies.poster,
        TableMovies.fanart,
        TableMovies.hearing_impaired,
        TableMovies.tmdb_id,
        TableMovies.audio_language,
        TableMovies.languages,
        fn.path_substitution_movie(TableMovies.path).alias('path'),
        TableMovies.subtitles,
        TableMovies.radarr_id,
        TableMovies.missing_subtitles,
        TableMovies.scene_name,
        TableMovies.monitored,
        TableMovies.failed_attempts,
        TableMovies.forced
    ).where(
        TableMovies.radarr_id % str(no)
    )

    for movie_details in movies_details:
        movies_details = movie_details
        break

    tmdbid = movies_details.tmdb_id

    languages = TableSettingsLanguages.select(
        TableSettingsLanguages.code2,
        TableSettingsLanguages.name
    ).where(
        TableSettingsLanguages.enabled == 1
    )

    return template('movie', bazarr_version=bazarr_version, no=no, details=movies_details,
                    languages=languages, url_radarr_short=url_radarr_short, base_url=base_url, tmdbid=tmdbid,
                    current_port=settings.general.port)


@route(base_url + 'scan_disk/<no:int>', method='GET')
@custom_auth_basic(check_credentials)
def scan_disk(no):
    authorize()
    ref = request.environ['HTTP_REFERER']
    
    series_scan_subtitles(no)
    
    redirect(ref)


@route(base_url + 'scan_disk_movie/<no:int>', method='GET')
@custom_auth_basic(check_credentials)
def scan_disk_movie(no):
    authorize()
    ref = request.environ['HTTP_REFERER']
    
    movies_scan_subtitles(no)
    
    redirect(ref)


@route(base_url + 'search_missing_subtitles/<no:int>', method='GET')
@custom_auth_basic(check_credentials)
def search_missing_subtitles(no):
    authorize()
    ref = request.environ['HTTP_REFERER']
    
    add_job(series_download_subtitles, args=[no], name=('search_missing_subtitles_' + str(no)))
    
    redirect(ref)


@route(base_url + 'search_missing_subtitles_movie/<no:int>', method='GET')
@custom_auth_basic(check_credentials)
def search_missing_subtitles_movie(no):
    authorize()
    ref = request.environ['HTTP_REFERER']
    
    add_job(movies_download_subtitles, args=[no], name=('movies_download_subtitles_' + str(no)))
    
    redirect(ref)


@route(base_url + 'history')
@custom_auth_basic(check_credentials)
def history():
    authorize()
    return template('history', bazarr_version=bazarr_version, base_url=base_url, current_port=settings.general.port)


@route(base_url + 'historyseries')
@custom_auth_basic(check_credentials)
def historyseries():
    authorize()

    row_count = TableHistory.select(

    ).join_from(
        TableHistory, TableShows, JOIN.LEFT_OUTER
    ).where(
        TableShows.title.is_null(False)
    ).count()
    page = request.GET.page
    if page == "":
        page = "1"
    page_size = int(settings.general.page_size)
    offset = (int(page) - 1) * page_size
    max_page = int(math.ceil(row_count / (page_size + 0.0)))
    
    now = datetime.now()
    today = []
    thisweek = []
    thisyear = []
    stats = TableHistory.select(
        TableHistory.timestamp
    ).where(
        TableHistory.action != 0
    )
    total = len(stats)
    for stat in stats:
        if now - timedelta(hours=24) <= datetime.fromtimestamp(stat.timestamp) <= now:
            today.append(datetime.fromtimestamp(stat.timestamp).date())
        if now - timedelta(weeks=1) <= datetime.fromtimestamp(stat.timestamp) <= now:
            thisweek.append(datetime.fromtimestamp(stat.timestamp).date())
        if now - timedelta(weeks=52) <= datetime.fromtimestamp(stat.timestamp) <= now:
            thisyear.append(datetime.fromtimestamp(stat.timestamp).date())
    stats = [len(today), len(thisweek), len(thisyear), total]

    data = TableHistory.select(
        TableHistory.action,
        TableShows.title.alias('seriesTitle'),
        TableEpisodes.season.cast('str').concat('x').concat(TableEpisodes.episode.cast('str')).alias('episode_number'),
        TableEpisodes.title.alias('episodeTitle'),
        TableHistory.timestamp,
        TableHistory.description,
        TableHistory.sonarr_series_id,
        TableEpisodes.path,
        TableShows.languages,
        TableHistory.language,
        TableHistory.score,
        TableShows.forced
    ).join_from(
        TableHistory, TableShows, JOIN.LEFT_OUTER
    ).join_from(
        TableHistory, TableEpisodes, JOIN.LEFT_OUTER
    ).where(
        TableShows.title.is_null(False)
    ).order_by(
        TableHistory.timestamp.desc()
    ).paginate(
        int(page),
        page_size
    ).objects()

    upgradable_episodes_not_perfect = []
    if settings.general.getboolean('upgrade_subs'):
        days_to_upgrade_subs = settings.general.days_to_upgrade_subs
        minimum_timestamp = ((datetime.now() - timedelta(days=int(days_to_upgrade_subs))) -
                             datetime(1970, 1, 1)).total_seconds()

        if settings.general.getboolean('upgrade_manual'):
            query_actions = [1, 2, 3]
        else:
            query_actions = [1, 3]

        episodes_details_clause = [
            (TableHistory.action.in_(query_actions)) &
            (TableHistory.score.is_null(False))
        ]

        if settings.sonarr.getboolean('only_monitored'):
            episodes_details_clause.append(
                (TableEpisodes.monitored == 'True')
            )

        upgradable_episodes = TableHistory.select(
            TableHistory.video_path,
            fn.MAX(TableHistory.timestamp).alias('timestamp'),
            TableHistory.score
        ).join_from(
            TableHistory, TableEpisodes, JOIN.LEFT_OUTER
        ).where(
            reduce(operator.and_, episodes_details_clause)
        ).group_by(
            TableHistory.video_path,
            TableHistory.language
        )

        for upgradable_episode in upgradable_episodes.dicts():
            if upgradable_episode['timestamp'] > minimum_timestamp:
                try:
                    int(upgradable_episode['score'])
                except ValueError:
                    pass
                else:
                    if int(upgradable_episode['score']) < 360:
                        upgradable_episodes_not_perfect.append(tuple(upgradable_episode.values()))
    
    return template('historyseries', bazarr_version=bazarr_version, rows=data, row_count=row_count,
                    page=page, max_page=max_page, stats=stats, base_url=base_url, page_size=page_size,
                    current_port=settings.general.port, upgradable_episodes=upgradable_episodes_not_perfect)


@route(base_url + 'historymovies')
@custom_auth_basic(check_credentials)
def historymovies():
    authorize()

    row_count = TableHistoryMovie.select(

    ).join_from(
        TableHistoryMovie, TableMovies, JOIN.LEFT_OUTER
    ).where(
        TableMovies.title.is_null(False)
    ).count()
    page = request.GET.page
    if page == "":
        page = "1"
    page_size = int(settings.general.page_size)
    offset = (int(page) - 1) * page_size
    max_page = int(math.ceil(row_count / (page_size + 0.0)))
    
    now = datetime.now()
    today = []
    thisweek = []
    thisyear = []
    stats = TableHistoryMovie.select(
        TableHistoryMovie.timestamp
    ).where(
        TableHistoryMovie.action > 0
    )
    total = len(stats)
    for stat in stats:
        if now - timedelta(hours=24) <= datetime.fromtimestamp(stat.timestamp) <= now:
            today.append(datetime.fromtimestamp(stat.timestamp).date())
        if now - timedelta(weeks=1) <= datetime.fromtimestamp(stat.timestamp) <= now:
            thisweek.append(datetime.fromtimestamp(stat.timestamp).date())
        if now - timedelta(weeks=52) <= datetime.fromtimestamp(stat.timestamp) <= now:
            thisyear.append(datetime.fromtimestamp(stat.timestamp).date())
    stats = [len(today), len(thisweek), len(thisyear), total]
    
    data = TableHistoryMovie.select(
        TableHistoryMovie.action,
        TableMovies.title,
        TableHistoryMovie.timestamp,
        TableHistoryMovie.description,
        TableHistoryMovie.radarr_id,
        TableHistoryMovie.video_path,
        TableMovies.languages,
        TableHistoryMovie.language,
        TableHistoryMovie.score,
        TableMovies.forced
    ).join_from(
        TableHistoryMovie, TableMovies, JOIN.LEFT_OUTER
    ).where(
        TableMovies.title.is_null(False)
    ).order_by(
        TableHistoryMovie.timestamp.desc()
    ).paginate(
        int(page),
        page_size
    ).objects()
    
    upgradable_movies = []
    upgradable_movies_not_perfect = []
    if settings.general.getboolean('upgrade_subs'):
        days_to_upgrade_subs = settings.general.days_to_upgrade_subs
        minimum_timestamp = ((datetime.now() - timedelta(days=int(days_to_upgrade_subs))) -
                             datetime(1970, 1, 1)).total_seconds()

        if settings.radarr.getboolean('only_monitored'):
            movies_monitored_only_query_string = ' AND table_movies.monitored = "True"'
        else:
            movies_monitored_only_query_string = ""

        if settings.general.getboolean('upgrade_manual'):
            query_actions = [1, 2, 3]
        else:
            query_actions = [1, 3]

        movies_details_clause = [
            (TableHistoryMovie.action.in_(query_actions)) &
            (TableHistoryMovie.score.is_null(False))
        ]

        if settings.radarr.getboolean('only_monitored'):
            movies_details_clause.append(
                (TableMovies.monitored == 'True')
            )

        upgradable_movies = TableHistoryMovie.select(
            TableHistoryMovie.video_path,
            fn.MAX(TableHistoryMovie.timestamp).alias('timestamp'),
            TableHistoryMovie.score
        ).join_from(
            TableHistoryMovie, TableMovies, JOIN.LEFT_OUTER
        ).where(
            reduce(operator.and_, movies_details_clause)
        ).group_by(
            TableHistoryMovie.video_path,
            TableHistoryMovie.language
        )

        for upgradable_movie in upgradable_movies.dicts():
            if upgradable_movie['timestamp'] > minimum_timestamp:
                try:
                    int(upgradable_movie['score'])
                except ValueError:
                    pass
                else:
                    if int(upgradable_movie['score']) < 120:
                        upgradable_movies_not_perfect.append(tuple(upgradable_movie.values()))

    return template('historymovies', bazarr_version=bazarr_version, rows=data, row_count=row_count,
                    page=page, max_page=max_page, stats=stats, base_url=base_url, page_size=page_size,
                    current_port=settings.general.port, upgradable_movies=upgradable_movies_not_perfect)


@route(base_url + 'wanted')
@custom_auth_basic(check_credentials)
def wanted():
    authorize()
    return template('wanted', bazarr_version=bazarr_version, base_url=base_url, current_port=settings.general.port)


@route(base_url + 'wantedseries')
@custom_auth_basic(check_credentials)
def wantedseries():
    authorize()

    missing_subtitles_clause = [
        (TableEpisodes.missing_subtitles != '[]')
    ]
    if settings.sonarr.getboolean('only_monitored'):
        missing_subtitles_clause.append(
            (TableEpisodes.monitored == 'True')
        )
    
    missing_count = TableEpisodes.select().where(
        reduce(operator.and_, missing_subtitles_clause)
    ).count()
    page = request.GET.page
    if page == "":
        page = "1"
    page_size = int(settings.general.page_size)
    offset = (int(page) - 1) * page_size
    max_page = int(math.ceil(missing_count / (page_size + 0.0)))
    
    data = TableEpisodes.select(
        TableShows.title.alias('seriesTitle'),
        TableEpisodes.season.cast('str').concat('x').concat(TableEpisodes.episode.cast('str')).alias('episode_number'),
        TableEpisodes.title.alias('episodeTitle'),
        TableEpisodes.missing_subtitles,
        TableEpisodes.sonarr_series_id,
        fn.path_substitution(TableEpisodes.path).alias('path'),
        TableShows.hearing_impaired,
        TableEpisodes.sonarr_episode_id,
        TableEpisodes.scene_name,
        TableEpisodes.failed_attempts
    ).join_from(
        TableEpisodes, TableShows, JOIN.LEFT_OUTER
    ).where(
        reduce(operator.and_, missing_subtitles_clause)
    ).order_by(
        TableEpisodes.sonarr_episode_id.desc()
    ).paginate(
        int(page),
        page_size
    ).objects()

    return template('wantedseries', bazarr_version=bazarr_version, rows=data, missing_count=missing_count, page=page,
                    max_page=max_page, base_url=base_url, page_size=page_size, current_port=settings.general.port)


@route(base_url + 'wantedmovies')
@custom_auth_basic(check_credentials)
def wantedmovies():
    authorize()

    missing_subtitles_clause = [
        (TableMovies.missing_subtitles != '[]')
    ]
    if settings.radarr.getboolean('only_monitored'):
        missing_subtitles_clause.append(
            (TableMovies.monitored == 'True')
        )

    missing_count = TableMovies.select().where(
        reduce(operator.and_, missing_subtitles_clause)
    ).count()
    page = request.GET.page
    if page == "":
        page = "1"
    page_size = int(settings.general.page_size)
    offset = (int(page) - 1) * page_size
    max_page = int(math.ceil(missing_count / (page_size + 0.0)))
    
    data = TableMovies.select(
        TableMovies.title,
        TableMovies.missing_subtitles,
        TableMovies.radarr_id,
        fn.path_substitution_movie(TableMovies.path).alias('path'),
        TableMovies.hearing_impaired,
        TableMovies.scene_name,
        TableMovies.failed_attempts
    ).where(
        reduce(operator.and_, missing_subtitles_clause)
    ).order_by(
        TableMovies.radarr_id.desc()
    ).paginate(
        int(page),
        page_size
    ).objects()

    return template('wantedmovies', bazarr_version=bazarr_version, rows=data,
                    missing_count=missing_count, page=page, max_page=max_page, base_url=base_url, page_size=page_size,
                    current_port=settings.general.port)


@route(base_url + 'wanted_search_missing_subtitles')
@custom_auth_basic(check_credentials)
def wanted_search_missing_subtitles_list():
    authorize()
    ref = request.environ['HTTP_REFERER']
    
    add_job(wanted_search_missing_subtitles, name='manual_wanted_search_missing_subtitles')
    
    redirect(ref)


@route(base_url + 'settings')
@custom_auth_basic(check_credentials)
def _settings():
    authorize()

    settings_languages = TableSettingsLanguages.select().order_by(TableSettingsLanguages.name)
    settings_providers = sorted(provider_manager.names())
    settings_notifier = TableSettingsNotifier.select().order_by(TableSettingsNotifier.name)

    return template('settings', bazarr_version=bazarr_version, settings=settings, settings_languages=settings_languages,
                    settings_providers=settings_providers, settings_notifier=settings_notifier, base_url=base_url,
                    current_port=settings.general.port)


@route(base_url + 'save_settings', method='POST')
@custom_auth_basic(check_credentials)
def save_settings():
    authorize()
    ref = request.environ['HTTP_REFERER']

    settings_general_ip = request.forms.get('settings_general_ip')
    settings_general_port = request.forms.get('settings_general_port')
    settings_general_baseurl = request.forms.get('settings_general_baseurl')
    if not settings_general_baseurl.endswith('/'):
        settings_general_baseurl += '/'
    settings_general_debug = request.forms.get('settings_general_debug')
    if settings_general_debug is None:
        settings_general_debug = 'False'
    else:
        settings_general_debug = 'True'
    settings_general_chmod_enabled = request.forms.get('settings_general_chmod_enabled')
    if settings_general_chmod_enabled is None:
        settings_general_chmod_enabled = 'False'
    else:
        settings_general_chmod_enabled = 'True'
    settings_general_chmod = request.forms.get('settings_general_chmod')
    settings_general_sourcepath = request.forms.getall('settings_general_sourcepath')
    settings_general_destpath = request.forms.getall('settings_general_destpath')
    settings_general_pathmapping = []
    settings_general_pathmapping.extend([list(a) for a in zip(settings_general_sourcepath, settings_general_destpath)])
    settings_general_sourcepath_movie = request.forms.getall('settings_general_sourcepath_movie')
    settings_general_destpath_movie = request.forms.getall('settings_general_destpath_movie')
    settings_general_pathmapping_movie = []
    settings_general_pathmapping_movie.extend(
        [list(a) for a in zip(settings_general_sourcepath_movie, settings_general_destpath_movie)])
    settings_general_branch = request.forms.get('settings_general_branch')
    settings_general_automatic = request.forms.get('settings_general_automatic')
    if settings_general_automatic is None:
        settings_general_automatic = 'False'
    else:
        settings_general_automatic = 'True'
    settings_general_update_restart = request.forms.get('settings_general_update_restart')
    if settings_general_update_restart is None:
        settings_general_update_restart = 'False'
    else:
        settings_general_update_restart = 'True'
    settings_analytics_enabled = request.forms.get('settings_analytics_enabled')
    if settings_analytics_enabled is None:
        settings_analytics_enabled = 'False'
    else:
        settings_analytics_enabled = 'True'
    settings_general_single_language = request.forms.get('settings_general_single_language')
    if settings_general_single_language is None:
        settings_general_single_language = 'False'
    else:
        settings_general_single_language = 'True'
    settings_general_wanted_search_frequency = request.forms.get('settings_general_wanted_search_frequency')
    settings_general_scenename = request.forms.get('settings_general_scenename')
    if settings_general_scenename is None:
        settings_general_scenename = 'False'
    else:
        settings_general_scenename = 'True'
    settings_general_embedded = request.forms.get('settings_general_embedded')
    if settings_general_embedded is None:
        settings_general_embedded = 'False'
    else:
        settings_general_embedded = 'True'
    settings_general_utf8_encode = request.forms.get('settings_general_utf8_encode')
    if settings_general_utf8_encode is None:
        settings_general_utf8_encode = 'False'
    else:
        settings_general_utf8_encode = 'True'
    settings_general_ignore_pgs = request.forms.get('settings_general_ignore_pgs')
    if settings_general_ignore_pgs is None:
        settings_general_ignore_pgs = 'False'
    else:
        settings_general_ignore_pgs = 'True'
    settings_general_adaptive_searching = request.forms.get('settings_general_adaptive_searching')
    if settings_general_adaptive_searching is None:
        settings_general_adaptive_searching = 'False'
    else:
        settings_general_adaptive_searching = 'True'
    settings_general_multithreading = request.forms.get('settings_general_multithreading')
    if settings_general_multithreading is None:
        settings_general_multithreading = 'False'
    else:
        settings_general_multithreading = 'True'
    settings_general_minimum_score = request.forms.get('settings_general_minimum_score')
    settings_general_minimum_score_movies = request.forms.get('settings_general_minimum_score_movies')
    settings_general_use_postprocessing = request.forms.get('settings_general_use_postprocessing')
    if settings_general_use_postprocessing is None:
        settings_general_use_postprocessing = 'False'
    else:
        settings_general_use_postprocessing = 'True'
    settings_general_postprocessing_cmd = request.forms.get('settings_general_postprocessing_cmd')
    settings_general_use_sonarr = request.forms.get('settings_general_use_sonarr')
    if settings_general_use_sonarr is None:
        settings_general_use_sonarr = 'False'
    else:
        settings_general_use_sonarr = 'True'
    settings_general_use_radarr = request.forms.get('settings_general_use_radarr')
    if settings_general_use_radarr is None:
        settings_general_use_radarr = 'False'
    else:
        settings_general_use_radarr = 'True'
    settings_page_size = request.forms.get('settings_page_size')
    settings_subfolder = request.forms.get('settings_subfolder')
    settings_subfolder_custom = request.forms.get('settings_subfolder_custom')
    settings_upgrade_subs = request.forms.get('settings_upgrade_subs')
    if settings_upgrade_subs is None:
        settings_upgrade_subs = 'False'
    else:
        settings_upgrade_subs = 'True'
    settings_upgrade_subs_frequency = request.forms.get('settings_upgrade_subs_frequency')
    settings_days_to_upgrade_subs = request.forms.get('settings_days_to_upgrade_subs')
    settings_upgrade_manual = request.forms.get('settings_upgrade_manual')
    if settings_upgrade_manual is None:
        settings_upgrade_manual = 'False'
    else:
        settings_upgrade_manual = 'True'
    settings_anti_captcha_provider = request.forms.get('settings_anti_captcha_provider')
    settings_anti_captcha_key = request.forms.get('settings_anti_captcha_key')
    settings_death_by_captcha_username = request.forms.get('settings_death_by_captcha_username')
    settings_death_by_captcha_password = request.forms.get('settings_death_by_captcha_password')
    
    before = (unicode(settings.general.ip), int(settings.general.port), unicode(settings.general.base_url),
              unicode(settings.general.path_mappings), unicode(settings.general.getboolean('use_sonarr')),
              unicode(settings.general.getboolean('use_radarr')), unicode(settings.general.path_mappings_movie))
    after = (unicode(settings_general_ip), int(settings_general_port), unicode(settings_general_baseurl),
             unicode(settings_general_pathmapping), unicode(settings_general_use_sonarr),
             unicode(settings_general_use_radarr), unicode(settings_general_pathmapping_movie))
    
    settings.general.ip = text_type(settings_general_ip)
    settings.general.port = text_type(settings_general_port)
    settings.general.base_url = text_type(settings_general_baseurl)
    settings.general.path_mappings = text_type(settings_general_pathmapping)
    settings.general.debug = text_type(settings_general_debug)
    settings.general.chmod_enabled = text_type(settings_general_chmod_enabled)
    settings.general.chmod = text_type(settings_general_chmod)
    settings.general.branch = text_type(settings_general_branch)
    settings.general.auto_update = text_type(settings_general_automatic)
    settings.general.update_restart = text_type(settings_general_update_restart)
    settings.analytics.enabled = text_type(settings_analytics_enabled)
    settings.general.single_language = text_type(settings_general_single_language)
    settings.general.minimum_score = text_type(settings_general_minimum_score)
    settings.general.wanted_search_frequency = text_type(settings_general_wanted_search_frequency)
    settings.general.use_scenename = text_type(settings_general_scenename)
    settings.general.use_postprocessing = text_type(settings_general_use_postprocessing)
    settings.general.postprocessing_cmd = text_type(settings_general_postprocessing_cmd)
    settings.general.use_sonarr = text_type(settings_general_use_sonarr)
    settings.general.use_radarr = text_type(settings_general_use_radarr)
    settings.general.path_mappings_movie = text_type(settings_general_pathmapping_movie)
    settings.general.page_size = text_type(settings_page_size)
    settings.general.subfolder = text_type(settings_subfolder)
    if settings.general.subfolder == 'current':
        settings.general.subfolder_custom = ''
    else:
        settings.general.subfolder_custom = text_type(settings_subfolder_custom)
    settings.general.upgrade_subs = text_type(settings_upgrade_subs)
    settings.general.upgrade_frequency = text_type(settings_upgrade_subs_frequency)
    settings.general.days_to_upgrade_subs = text_type(settings_days_to_upgrade_subs)
    settings.general.upgrade_manual = text_type(settings_upgrade_manual)
    settings.general.anti_captcha_provider = text_type(settings_anti_captcha_provider)
    settings.anticaptcha.anti_captcha_key = text_type(settings_anti_captcha_key)
    settings.deathbycaptcha.username = text_type(settings_death_by_captcha_username)
    settings.deathbycaptcha.password = text_type(settings_death_by_captcha_password)
    
    # set anti-captcha provider and key
    if settings.general.anti_captcha_provider == 'anti-captcha':
        os.environ["ANTICAPTCHA_CLASS"] = 'AntiCaptchaProxyLess'
        os.environ["ANTICAPTCHA_ACCOUNT_KEY"] = settings.anticaptcha.anti_captcha_key
    elif settings.general.anti_captcha_provider == 'death-by-captcha':
        os.environ["ANTICAPTCHA_CLASS"] = 'DeathByCaptchaProxyLess'
        os.environ["ANTICAPTCHA_ACCOUNT_KEY"] = ':'.join(
            {settings.deathbycaptcha.username, settings.deathbycaptcha.password})
    else:
        os.environ["ANTICAPTCHA_CLASS"] = ''
    
    settings.general.minimum_score_movie = text_type(settings_general_minimum_score_movies)
    settings.general.use_embedded_subs = text_type(settings_general_embedded)
    settings.general.utf8_encode = text_type(settings_general_utf8_encode)
    settings.general.ignore_pgs_subs = text_type(settings_general_ignore_pgs)
    settings.general.adaptive_searching = text_type(settings_general_adaptive_searching)
    settings.general.multithreading = text_type(settings_general_multithreading)
    
    if after != before:
        configured()
    
    settings_proxy_type = request.forms.get('settings_proxy_type')
    settings_proxy_url = request.forms.get('settings_proxy_url')
    settings_proxy_port = request.forms.get('settings_proxy_port')
    settings_proxy_username = request.forms.get('settings_proxy_username')
    settings_proxy_password = request.forms.get('settings_proxy_password')
    settings_proxy_exclude = request.forms.get('settings_proxy_exclude')
    
    before_proxy_password = (unicode(settings.proxy.type), unicode(settings.proxy.exclude))
    if before_proxy_password[0] != settings_proxy_type:
        configured()
    if before_proxy_password[1] == settings_proxy_password:
        settings.proxy.type = text_type(settings_proxy_type)
        settings.proxy.url = text_type(settings_proxy_url)
        settings.proxy.port = text_type(settings_proxy_port)
        settings.proxy.username = text_type(settings_proxy_username)
        settings.proxy.exclude = text_type(settings_proxy_exclude)
    else:
        settings.proxy.type = text_type(settings_proxy_type)
        settings.proxy.url = text_type(settings_proxy_url)
        settings.proxy.port = text_type(settings_proxy_port)
        settings.proxy.username = text_type(settings_proxy_username)
        settings.proxy.password = text_type(settings_proxy_password)
        settings.proxy.exclude = text_type(settings_proxy_exclude)
    
    settings_auth_type = request.forms.get('settings_auth_type')
    settings_auth_username = request.forms.get('settings_auth_username')
    settings_auth_password = request.forms.get('settings_auth_password')
    
    if settings.auth.type != settings_auth_type:
        configured()
    if settings.auth.password == settings_auth_password:
        settings.auth.type = text_type(settings_auth_type)
        settings.auth.username = text_type(settings_auth_username)
    else:
        settings.auth.type = text_type(settings_auth_type)
        settings.auth.username = text_type(settings_auth_username)
        settings.auth.password = hashlib.md5(settings_auth_password).hexdigest()
    if settings_auth_username not in aaa._store.users:
        cork = Cork(os.path.normpath(os.path.join(args.config_dir, 'config')), initialize=True)
        cork._store.roles[''] = 100
        cork._store.save_roles()
        cork._store.users[settings_auth_username] = {
            'role': '',
            'hash': cork._hash(settings_auth_username, settings_auth_password),
            'email_addr': '',
            'desc': '',
            'creation_date': time.time()
        }
        cork._store.save_users()
        if settings_auth_type == 'basic' or settings_auth_type == 'None':
            pass
        else:
            aaa._beaker_session.delete()
    else:
        if settings.auth.password != settings_auth_password:
            aaa.user(settings_auth_username).update(role='', pwd=settings_auth_password)
            if settings_auth_type == 'basic' or settings_auth_type == 'None':
                pass
            else:
                aaa._beaker_session.delete()
    
    settings_sonarr_ip = request.forms.get('settings_sonarr_ip')
    settings_sonarr_port = request.forms.get('settings_sonarr_port')
    settings_sonarr_baseurl = request.forms.get('settings_sonarr_baseurl')
    settings_sonarr_ssl = request.forms.get('settings_sonarr_ssl')
    if settings_sonarr_ssl is None:
        settings_sonarr_ssl = 'False'
    else:
        settings_sonarr_ssl = 'True'
    settings_sonarr_apikey = request.forms.get('settings_sonarr_apikey')
    settings_sonarr_only_monitored = request.forms.get('settings_sonarr_only_monitored')
    if settings_sonarr_only_monitored is None:
        settings_sonarr_only_monitored = 'False'
    else:
        settings_sonarr_only_monitored = 'True'
    settings_sonarr_sync = request.forms.get('settings_sonarr_sync')
    settings_sonarr_sync_day = request.forms.get('settings_sonarr_sync_day')
    settings_sonarr_sync_hour = request.forms.get('settings_sonarr_sync_hour')

    settings.sonarr.ip = text_type(settings_sonarr_ip)
    settings.sonarr.port = text_type(settings_sonarr_port)
    settings.sonarr.base_url = text_type(settings_sonarr_baseurl)
    settings.sonarr.ssl = text_type(settings_sonarr_ssl)
    settings.sonarr.apikey = text_type(settings_sonarr_apikey)
    settings.sonarr.only_monitored = text_type(settings_sonarr_only_monitored)
    settings.sonarr.full_update = text_type(settings_sonarr_sync)
    settings.sonarr.full_update_day = text_type(settings_sonarr_sync_day)
    settings.sonarr.full_update_hour = text_type(settings_sonarr_sync_hour)

    settings_radarr_ip = request.forms.get('settings_radarr_ip')
    settings_radarr_port = request.forms.get('settings_radarr_port')
    settings_radarr_baseurl = request.forms.get('settings_radarr_baseurl')
    settings_radarr_ssl = request.forms.get('settings_radarr_ssl')
    if settings_radarr_ssl is None:
        settings_radarr_ssl = 'False'
    else:
        settings_radarr_ssl = 'True'
    settings_radarr_apikey = request.forms.get('settings_radarr_apikey')
    settings_radarr_only_monitored = request.forms.get('settings_radarr_only_monitored')
    if settings_radarr_only_monitored is None:
        settings_radarr_only_monitored = 'False'
    else:
        settings_radarr_only_monitored = 'True'
    settings_radarr_sync = request.forms.get('settings_radarr_sync')
    settings_radarr_sync_day = request.forms.get('settings_radarr_sync_day')
    settings_radarr_sync_hour = request.forms.get('settings_radarr_sync_hour')

    settings.radarr.ip = text_type(settings_radarr_ip)
    settings.radarr.port = text_type(settings_radarr_port)
    settings.radarr.base_url = text_type(settings_radarr_baseurl)
    settings.radarr.ssl = text_type(settings_radarr_ssl)
    settings.radarr.apikey = text_type(settings_radarr_apikey)
    settings.radarr.only_monitored = text_type(settings_radarr_only_monitored)
    settings.radarr.full_update = text_type(settings_radarr_sync)
    settings.radarr.full_update_day = text_type(settings_radarr_sync_day)
    settings.radarr.full_update_hour = text_type(settings_radarr_sync_hour)

    settings_subliminal_providers = request.forms.getall('settings_subliminal_providers')
    settings.general.enabled_providers = u'' if not settings_subliminal_providers else ','.join(
        settings_subliminal_providers)
    
    settings_addic7ed_random_agents = request.forms.get('settings_addic7ed_random_agents')
    if settings_addic7ed_random_agents is None:
        settings_addic7ed_random_agents = 'False'
    else:
        settings_addic7ed_random_agents = 'True'
    
    settings_opensubtitles_vip = request.forms.get('settings_opensubtitles_vip')
    if settings_opensubtitles_vip is None:
        settings_opensubtitles_vip = 'False'
    else:
        settings_opensubtitles_vip = 'True'
    
    settings_opensubtitles_ssl = request.forms.get('settings_opensubtitles_ssl')
    if settings_opensubtitles_ssl is None:
        settings_opensubtitles_ssl = 'False'
    else:
        settings_opensubtitles_ssl = 'True'
    
    settings_opensubtitles_skip_wrong_fps = request.forms.get('settings_opensubtitles_skip_wrong_fps')
    if settings_opensubtitles_skip_wrong_fps is None:
        settings_opensubtitles_skip_wrong_fps = 'False'
    else:
        settings_opensubtitles_skip_wrong_fps = 'True'
    
    settings.addic7ed.username = request.forms.get('settings_addic7ed_username')
    settings.addic7ed.password = request.forms.get('settings_addic7ed_password')
    settings.addic7ed.random_agents = text_type(settings_addic7ed_random_agents)
    settings.assrt.token = request.forms.get('settings_assrt_token')
    settings.legendastv.username = request.forms.get('settings_legendastv_username')
    settings.legendastv.password = request.forms.get('settings_legendastv_password')
    settings.opensubtitles.username = request.forms.get('settings_opensubtitles_username')
    settings.opensubtitles.password = request.forms.get('settings_opensubtitles_password')
    settings.opensubtitles.vip = text_type(settings_opensubtitles_vip)
    settings.opensubtitles.ssl = text_type(settings_opensubtitles_ssl)
    settings.opensubtitles.skip_wrong_fps = text_type(settings_opensubtitles_skip_wrong_fps)
    settings.xsubs.username = request.forms.get('settings_xsubs_username')
    settings.xsubs.password = request.forms.get('settings_xsubs_password')
    settings.napisy24.username = request.forms.get('settings_napisy24_username')
    settings.napisy24.password = request.forms.get('settings_napisy24_password')
    settings.subscene.username = request.forms.get('settings_subscene_username')
    settings.subscene.password = request.forms.get('settings_subscene_password')
    settings.titlovi.username = request.forms.get('settings_titlovi_username')
    settings.titlovi.password = request.forms.get('settings_titlovi_password')
    settings.betaseries.token = request.forms.get('settings_betaseries_token')

    settings_subliminal_languages = request.forms.getall('settings_subliminal_languages')
    TableSettingsLanguages.update({TableSettingsLanguages.enabled: 0}).execute()
    for item in settings_subliminal_languages:
        TableSettingsLanguages.update(
            {
                TableSettingsLanguages.enabled: 1
            }
        ).where(
            TableSettingsLanguages.code2 == item
        ).execute()
    
    settings_serie_default_enabled = request.forms.get('settings_serie_default_enabled')
    if settings_serie_default_enabled is None:
        settings_serie_default_enabled = 'False'
    else:
        settings_serie_default_enabled = 'True'
    settings.general.serie_default_enabled = text_type(settings_serie_default_enabled)
    
    settings_serie_default_languages = str(request.forms.getall('settings_serie_default_languages'))
    if settings_serie_default_languages == "['None']":
        settings_serie_default_languages = 'None'
    settings.general.serie_default_language = text_type(settings_serie_default_languages)
    
    settings_serie_default_hi = request.forms.get('settings_serie_default_hi')
    if settings_serie_default_hi is None:
        settings_serie_default_hi = 'False'
    else:
        settings_serie_default_hi = 'True'
    settings.general.serie_default_hi = text_type(settings_serie_default_hi)
    
    settings_serie_default_forced = str(request.forms.get('settings_serie_default_forced'))
    settings.general.serie_default_forced = text_type(settings_serie_default_forced)
    
    settings_movie_default_enabled = request.forms.get('settings_movie_default_enabled')
    if settings_movie_default_enabled is None:
        settings_movie_default_enabled = 'False'
    else:
        settings_movie_default_enabled = 'True'
    settings.general.movie_default_enabled = text_type(settings_movie_default_enabled)
    
    settings_movie_default_languages = str(request.forms.getall('settings_movie_default_languages'))
    if settings_movie_default_languages == "['None']":
        settings_movie_default_languages = 'None'
    settings.general.movie_default_language = text_type(settings_movie_default_languages)
    
    settings_movie_default_hi = request.forms.get('settings_movie_default_hi')
    if settings_movie_default_hi is None:
        settings_movie_default_hi = 'False'
    else:
        settings_movie_default_hi = 'True'
    settings.general.movie_default_hi = text_type(settings_movie_default_hi)
    
    settings_movie_default_forced = str(request.forms.get('settings_movie_default_forced'))
    settings.general.movie_default_forced = text_type(settings_movie_default_forced)
    
    with open(os.path.join(args.config_dir, 'config', 'config.ini'), 'w+') as handle:
        settings.write(handle)
    
    configure_logging(settings.general.getboolean('debug') or args.debug)
    
    notifiers = TableSettingsNotifier.select().order_by(TableSettingsNotifier.name)
    for notifier in notifiers:
        enabled = request.forms.get('settings_notifier_' + notifier.name + '_enabled')
        if enabled == 'on':
            enabled = 1
        else:
            enabled = 0
        notifier_url = request.forms.get('settings_notifier_' + notifier.name + '_url')
        TableSettingsNotifier.update(
            {
                TableSettingsNotifier.enabled: enabled,
                TableSettingsNotifier.url: notifier_url
            }
        ).where(
            TableSettingsNotifier.name == notifier.name
        ).execute()
    
    schedule_update_job()
    sonarr_full_update()
    radarr_full_update()
    schedule_wanted_search()
    schedule_upgrade_subs()
    
    logging.info('BAZARR Settings saved succesfully.')
    
    if ref.find('saved=true') > 0:
        redirect(ref)
    else:
        redirect(ref + "?saved=true")


@route(base_url + 'check_update')
@custom_auth_basic(check_credentials)
def check_update():
    authorize()
    ref = request.environ['HTTP_REFERER']
    
    if not args.no_update:
        check_and_apply_update()
    
    redirect(ref)


@route(base_url + 'system')
@custom_auth_basic(check_credentials)
def system():
    authorize()

    def get_time_from_interval(td_object):
        seconds = int(td_object.total_seconds())
        periods = [
            ('year', 60 * 60 * 24 * 365),
            ('month', 60 * 60 * 24 * 30),
            ('day', 60 * 60 * 24),
            ('hour', 60 * 60),
            ('minute', 60),
            ('second', 1)
        ]
        
        strings = []
        for period_name, period_seconds in periods:
            if seconds > period_seconds:
                period_value, seconds = divmod(seconds, period_seconds)
                has_s = 's' if period_value > 1 else ''
                strings.append("%s %s%s" % (period_value, period_name, has_s))
        
        return ", ".join(strings)

    def get_time_from_cron(cron):
        year = str(cron[0])
        if year == "2100":
            return "Never"

        day = str(cron[4])
        hour = str(cron[5])
        text = ""

        if day == "*":
            text = "everyday"
        else:
            text = "every " + day_name[int(day)]

        if hour != "*":
            text += " at " + hour + ":00"

        return text

    task_list = []
    for job in scheduler.get_jobs():
        if isinstance(job.trigger, CronTrigger):
            if str(job.trigger.__getstate__()['fields'][0]) == "2100":
                next_run = 'Never'
            else:
                next_run = pretty.date(job.next_run_time.replace(tzinfo=None))
        else:
            next_run = pretty.date(job.next_run_time.replace(tzinfo=None))
        
        if isinstance(job.trigger, IntervalTrigger):
            interval = "every " + get_time_from_interval(job.trigger.__getstate__()['interval'])
            task_list.append([job.name, interval, next_run, job.id])
        elif isinstance(job.trigger, CronTrigger):
            task_list.append([job.name, get_time_from_cron(job.trigger.fields), next_run, job.id])
    
    throttled_providers = list_throttled_providers()
    
    try:
        with open(os.path.join(args.config_dir, 'config', 'releases.txt'), 'r') as f:
            releases = ast.literal_eval(f.read())
    except Exception as e:
        releases = []
        logging.exception(
            'BAZARR cannot parse releases caching file: ' + os.path.join(args.config_dir, 'config', 'releases.txt'))
    
    sonarr_version = get_sonarr_version()
    
    radarr_version = get_radarr_version()
    
    page_size = int(settings.general.page_size)
    
    return template('system', bazarr_version=bazarr_version,
                    sonarr_version=sonarr_version, radarr_version=radarr_version,
                    operating_system=platform.platform(), python_version=platform.python_version(),
                    config_dir=args.config_dir, bazarr_dir=os.path.normcase(os.path.dirname(os.path.dirname(__file__))),
                    base_url=base_url, task_list=task_list, page_size=page_size, releases=releases,
                    current_port=settings.general.port, throttled_providers=throttled_providers)


@route(base_url + 'logs')
@custom_auth_basic(check_credentials)
def get_logs():
    authorize()
    logs = []
    for line in reversed(open(os.path.join(args.config_dir, 'log', 'bazarr.log')).readlines()):
        lin = []
        lin = line.split('|')
        logs.append(lin)
    
    return dict(data=logs)


@route(base_url + 'execute/<taskid>')
@custom_auth_basic(check_credentials)
def execute_task(taskid):
    authorize()
    ref = request.environ['HTTP_REFERER']
    
    execute_now(taskid)
    
    redirect(ref)


@route(base_url + 'remove_subtitles', method='POST')
@custom_auth_basic(check_credentials)
def remove_subtitles():
    authorize()
    episodePath = request.forms.get('episodePath')
    language = request.forms.get('language')
    subtitlesPath = request.forms.get('subtitlesPath')
    sonarrSeriesId = request.forms.get('sonarrSeriesId')
    sonarrEpisodeId = request.forms.get('sonarrEpisodeId')
    
    try:
        os.remove(subtitlesPath)
        result = language_from_alpha3(language) + " subtitles deleted from disk."
        history_log(0, sonarrSeriesId, sonarrEpisodeId, result)
    except OSError as e:
        logging.exception('BAZARR cannot delete subtitles file: ' + subtitlesPath)
    store_subtitles(unicode(episodePath))
    list_missing_subtitles(sonarrSeriesId)


@route(base_url + 'remove_subtitles_movie', method='POST')
@custom_auth_basic(check_credentials)
def remove_subtitles_movie():
    authorize()
    moviePath = request.forms.get('moviePath')
    language = request.forms.get('language')
    subtitlesPath = request.forms.get('subtitlesPath')
    radarrId = request.forms.get('radarrId')
    
    try:
        os.remove(subtitlesPath)
        result = language_from_alpha3(language) + " subtitles deleted from disk."
        history_log_movie(0, radarrId, result)
    except OSError as e:
        logging.exception('BAZARR cannot delete subtitles file: ' + subtitlesPath)
    store_subtitles_movie(unicode(moviePath))
    list_missing_subtitles_movies(radarrId)


@route(base_url + 'get_subtitle', method='POST')
@custom_auth_basic(check_credentials)
def get_subtitle():
    authorize()
    ref = request.environ['HTTP_REFERER']
    
    episodePath = request.forms.get('episodePath')
    sceneName = request.forms.get('sceneName')
    language = request.forms.get('language')
    hi = request.forms.get('hi')
    forced = request.forms.get('forced')
    sonarrSeriesId = request.forms.get('sonarrSeriesId')
    sonarrEpisodeId = request.forms.get('sonarrEpisodeId')
    title = request.forms.get('title')
    
    providers_list = get_providers()
    providers_auth = get_providers_auth()
    
    try:
        result = download_subtitle(episodePath, language, hi, forced, providers_list, providers_auth, sceneName, title,
                                   'series')
        if result is not None:
            message = result[0]
            path = result[1]
            forced = result[5]
            language_code = result[2] + ":forced" if forced else result[2]
            provider = result[3]
            score = result[4]
            history_log(1, sonarrSeriesId, sonarrEpisodeId, message, path, language_code, provider, score)
            send_notifications(sonarrSeriesId, sonarrEpisodeId, message)
            store_subtitles(unicode(episodePath))
            list_missing_subtitles(sonarrSeriesId)
        redirect(ref)
    except OSError:
        pass


@route(base_url + 'manual_search', method='POST')
@custom_auth_basic(check_credentials)
def manual_search_json():
    authorize()
    
    episodePath = request.forms.get('episodePath')
    sceneName = request.forms.get('sceneName')
    language = request.forms.get('language')
    hi = request.forms.get('hi')
    forced = request.forms.get('forced')
    title = request.forms.get('title')
    
    providers_list = get_providers()
    providers_auth = get_providers_auth()
    
    data = manual_search(episodePath, language, hi, forced, providers_list, providers_auth, sceneName, title, 'series')
    return dict(data=data)


@route(base_url + 'manual_get_subtitle', method='POST')
@custom_auth_basic(check_credentials)
def manual_get_subtitle():
    authorize()
    ref = request.environ['HTTP_REFERER']
    
    episodePath = request.forms.get('episodePath')
    sceneName = request.forms.get('sceneName')
    language = request.forms.get('language')
    hi = request.forms.get('hi')
    forced = request.forms.get('forced')
    selected_provider = request.forms.get('provider')
    subtitle = request.forms.get('subtitle')
    sonarrSeriesId = request.forms.get('sonarrSeriesId')
    sonarrEpisodeId = request.forms.get('sonarrEpisodeId')
    title = request.forms.get('title')
    
    providers_auth = get_providers_auth()
    
    try:
        result = manual_download_subtitle(episodePath, language, hi, forced, subtitle, selected_provider,
                                          providers_auth,
                                          sceneName, title, 'series')
        if result is not None:
            message = result[0]
            path = result[1]
            forced = result[5]
            language_code = result[2] + ":forced" if forced else result[2]
            provider = result[3]
            score = result[4]
            history_log(2, sonarrSeriesId, sonarrEpisodeId, message, path, language_code, provider, score)
            send_notifications(sonarrSeriesId, sonarrEpisodeId, message)
            store_subtitles(unicode(episodePath))
            list_missing_subtitles(sonarrSeriesId)
        redirect(ref)
    except OSError:
        pass


@route(base_url + 'manual_upload_subtitle', method='POST')
@custom_auth_basic(check_credentials)
def perform_manual_upload_subtitle():
    authorize()
    ref = request.environ['HTTP_REFERER']

    episodePath = request.forms.get('episodePath')
    sceneName = request.forms.get('sceneName')
    language = request.forms.get('language')
    forced = True if request.forms.get('forced') == '1' else False
    upload = request.files.get('upload')
    sonarrSeriesId = request.forms.get('sonarrSeriesId')
    sonarrEpisodeId = request.forms.get('sonarrEpisodeId')
    title = request.forms.get('title')

    _, ext = os.path.splitext(upload.filename)

    if ext not in SUBTITLE_EXTENSIONS:
        raise ValueError('A subtitle of an invalid format was uploaded.')

    try:
        result = manual_upload_subtitle(path=episodePath,
                                        language=language,
                                        forced=forced,
                                        title=title,
                                        scene_name=sceneName,
                                        media_type='series',
                                        subtitle=upload)

        if result is not None:
            message = result[0]
            path = result[1]
            language_code = language + ":forced" if forced else language
            provider = "manual"
            score = 360
            history_log(4, sonarrSeriesId, sonarrEpisodeId, message, path, language_code, provider, score)
            send_notifications(sonarrSeriesId, sonarrEpisodeId, message)
            store_subtitles(unicode(episodePath))
            list_missing_subtitles(sonarrSeriesId)

        redirect(ref)
    except OSError:
        pass


@route(base_url + 'get_subtitle_movie', method='POST')
@custom_auth_basic(check_credentials)
def get_subtitle_movie():
    authorize()
    ref = request.environ['HTTP_REFERER']
    
    moviePath = request.forms.get('moviePath')
    sceneName = request.forms.get('sceneName')
    language = request.forms.get('language')
    hi = request.forms.get('hi')
    forced = request.forms.get('forced')
    radarrId = request.forms.get('radarrId')
    title = request.forms.get('title')
    
    providers_list = get_providers()
    providers_auth = get_providers_auth()
    
    try:
        result = download_subtitle(moviePath, language, hi, forced, providers_list, providers_auth, sceneName, title,
                                   'movie')
        if result is not None:
            message = result[0]
            path = result[1]
            forced = result[5]
            language_code = result[2] + ":forced" if forced else result[2]
            provider = result[3]
            score = result[4]
            history_log_movie(1, radarrId, message, path, language_code, provider, score)
            send_notifications_movie(radarrId, message)
            store_subtitles_movie(unicode(moviePath))
            list_missing_subtitles_movies(radarrId)
        redirect(ref)
    except OSError:
        pass


@route(base_url + 'manual_search_movie', method='POST')
@custom_auth_basic(check_credentials)
def manual_search_movie_json():
    authorize()
    
    moviePath = request.forms.get('moviePath')
    sceneName = request.forms.get('sceneName')
    language = request.forms.get('language')
    hi = request.forms.get('hi')
    forced = request.forms.get('forced')
    title = request.forms.get('title')
    
    providers_list = get_providers()
    providers_auth = get_providers_auth()
    
    data = manual_search(moviePath, language, hi, forced, providers_list, providers_auth, sceneName, title, 'movie')
    return dict(data=data)


@route(base_url + 'manual_get_subtitle_movie', method='POST')
@custom_auth_basic(check_credentials)
def manual_get_subtitle_movie():
    authorize()
    ref = request.environ['HTTP_REFERER']
    
    moviePath = request.forms.get('moviePath')
    sceneName = request.forms.get('sceneName')
    language = request.forms.get('language')
    hi = request.forms.get('hi')
    forced = request.forms.get('forced')
    selected_provider = request.forms.get('provider')
    subtitle = request.forms.get('subtitle')
    radarrId = request.forms.get('radarrId')
    title = request.forms.get('title')
    
    providers_auth = get_providers_auth()
    
    try:
        result = manual_download_subtitle(moviePath, language, hi, forced, subtitle, selected_provider, providers_auth,
                                          sceneName, title, 'movie')
        if result is not None:
            message = result[0]
            path = result[1]
            forced = result[5]
            language_code = result[2] + ":forced" if forced else result[2]
            provider = result[3]
            score = result[4]
            history_log_movie(2, radarrId, message, path, language_code, provider, score)
            send_notifications_movie(radarrId, message)
            store_subtitles_movie(unicode(moviePath))
            list_missing_subtitles_movies(radarrId)
        redirect(ref)
    except OSError:
        pass


@route(base_url + 'manual_upload_subtitle_movie', method='POST')
@custom_auth_basic(check_credentials)
def perform_manual_upload_subtitle_movie():
    authorize()
    ref = request.environ['HTTP_REFERER']

    moviePath = request.forms.get('moviePath')
    sceneName = request.forms.get('sceneName')
    language = request.forms.get('language')
    forced = True if request.forms.get('forced') == '1' else False
    upload = request.files.get('upload')
    radarrId = request.forms.get('radarrId')
    title = request.forms.get('title')

    _, ext = os.path.splitext(upload.filename)

    if ext not in SUBTITLE_EXTENSIONS:
        raise ValueError('A subtitle of an invalid format was uploaded.')

    try:
        result = manual_upload_subtitle(path=moviePath,
                                        language=language,
                                        forced=forced,
                                        title=title,
                                        scene_name=sceneName,
                                        media_type='series',
                                        subtitle=upload)

        if result is not None:
            message = result[0]
            path = result[1]
            language_code = language + ":forced" if forced else language
            provider = "manual"
            score = 120
            history_log_movie(4, radarrId, message, path, language_code, provider, score)
            send_notifications_movie(radarrId, message)
            store_subtitles_movie(unicode(moviePath))
            list_missing_subtitles_movies(radarrId)

        redirect(ref)
    except OSError:
        pass


def configured():
    System.update({System.configured: 1}).execute()


@route(base_url + 'api/series/wanted')
def api_wanted():
    data = TableEpisodes.select(
        TableShows.title.alias('seriesTitle'),
        TableEpisodes.season.cast('str').concat('x').concat(TableEpisodes.episode.cast('str')).alias('episode_number'),
        TableEpisodes.title.alias('episodeTitle'),
        TableEpisodes.missing_subtitles
    ).join_from(
        TableEpisodes, TableShows, JOIN.LEFT_OUTER
    ).where(
        TableEpisodes.missing_subtitles != '[]'
    ).order_by(
        TableEpisodes.sonarr_episode_id.desc()
    ).limit(10).objects()

    wanted_subs = []
    for item in data:
        wanted_subs.append([item.seriesTitle, item.episode_number, item.episodeTitle, item.missing_subtitles])

    return dict(subtitles=wanted_subs)


@route(base_url + 'api/series/history')
def api_history():
    data = TableHistory.select(
        TableShows.title.alias('seriesTitle'),
        TableEpisodes.season.cast('str').concat('x').concat(TableEpisodes.episode.cast('str')).alias('episode_number'),
        TableEpisodes.title.alias('episodeTitle'),
        fn.strftime('%Y-%m-%d', fn.datetime(TableHistory.timestamp, 'unixepoch')).alias('date'),
        TableHistory.description
    ).join_from(
        TableHistory, TableShows, JOIN.LEFT_OUTER
    ).join_from(
        TableHistory, TableEpisodes, JOIN. LEFT_OUTER
    ).where(
        TableHistory.action != '0'
    ).order_by(
        TableHistory.id.desc()
    ).limit(10).objects()

    history_subs = []
    for item in data:
        history_subs.append([item.seriesTitle, item.episode_number, item.episodeTitle, item.date, item.description])

    return dict(subtitles=history_subs)


@route(base_url + 'api/movies/wanted')
def api_wanted():
    data = TableMovies.select(
        TableMovies.title,
        TableMovies.missing_subtitles
    ).where(
        TableMovies.missing_subtitles != '[]'
    ).order_by(
        TableMovies.radarr_id.desc()
    ).limit(10).objects()

    wanted_subs = []
    for item in data:
        wanted_subs.append([item.title, item.missing_subtitles])

    return dict(subtitles=wanted_subs)


@route(base_url + 'api/movies/history')
def api_history():
    data = TableHistoryMovie.select(
        TableMovies.title,
        fn.strftime('%Y-%m-%d', fn.datetime(TableHistoryMovie.timestamp, 'unixepoch')).alias('date'),
        TableHistoryMovie.description
    ).join_from(
        TableHistoryMovie, TableMovies, JOIN.LEFT_OUTER
    ).where(
        TableHistoryMovie.action != '0'
    ).order_by(
        TableHistoryMovie.id.desc()
    ).limit(10).objects()

    history_subs = []
    for item in data:
        history_subs.append([item.title, item.date, item.description])

    return dict(subtitles=history_subs)


@route(base_url + 'test_url/<protocol>/<url:path>', method='GET')
@custom_auth_basic(check_credentials)
def test_url(protocol, url):
    url = urllib.unquote(url)
    try:
        result = requests.get(protocol + "://" + url, allow_redirects=False, verify=False).json()['version']
    except:
        return dict(status=False)
    else:
        return dict(status=True, version=result)


@route(base_url + 'test_notification/<protocol>/<provider:path>', method='GET')
@custom_auth_basic(check_credentials)
def test_notification(protocol, provider):
    provider = urllib.unquote(provider)
    apobj = apprise.Apprise()
    apobj.add(protocol + "://" + provider)
    
    apobj.notify(
        title='Bazarr test notification',
        body=('Test notification')
    )


@route(base_url + 'notifications')
@custom_auth_basic(check_credentials)
def notifications():
    if queueconfig.notifications:
        return queueconfig.notifications.read()
    else:
        return None


@route(base_url + 'running_tasks')
@custom_auth_basic(check_credentials)
def running_tasks_list():
    return dict(tasks=running_tasks)


# Mute DeprecationWarning
warnings.simplefilter("ignore", DeprecationWarning)
server = CherryPyWSGIServer((str(settings.general.ip), (int(args.port) if args.port else int(settings.general.port))), app)
try:
    logging.info('BAZARR is started and waiting for request on http://' + str(settings.general.ip) + ':' + (str(
        args.port) if args.port else str(settings.general.port)) + str(base_url))
    server.start()
except KeyboardInterrupt:
    shutdown()
