# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 TheIntroDB
#
# subclasses xbmc.player — path, whether we treat this as tv-ish content, scrape ids for api
import json
import re
import xbmc
import xbmcaddon
import xbmcgui
from typing import Optional, Dict, Any, Tuple

import addon_settings

ADDON = xbmcaddon.Addon()

def try_int(value: Any) -> Optional[int]:
    """Loose conversion to int for infoLabel comparisons; None when not numeric."""
    try:
        return int(str(value).strip())
    except (ValueError, TypeError):
        return None

# TMDbHelper publishes what is playing on Window(10000): its player monitor
# resolves the played item and sets these properties, and its own play flow
# leaves the ids of the item that was started in PlayerInfoString. Reading them
# lets library-less playback through external players be identified without
# touching those add-ons.
_TMDBHELPER_WINDOW = 10000
_TMDBHELPER_PREFIX = 'TMDbHelper.Player.'


class TIDBPlayer(xbmc.Player):
    def __init__(self) -> None:
        super(TIDBPlayer, self).__init__()
        self._playback_started: bool = False
        self._filename: Optional[str] = None
        self._is_tv: bool = False
        self._is_video: bool = False
        self._is_paused: bool = False
        self._pause_count: int = 0

    @property
    def playback_started(self) -> bool:
        return self._playback_started

    @property
    def filename(self) -> Optional[str]:
        return self._filename

    @property
    def is_tv_content(self) -> bool:
        return self._is_tv

    @property
    def is_video(self) -> bool:
        return self._is_video

    @property
    def is_paused(self) -> bool:
        return self._is_paused

    @property
    def pause_count(self) -> int:
        return self._pause_count

    def onAVStarted(self) -> None:
        self._playback_started = True
        try:
            self._filename = self.getPlayingFile()
        except Exception:
            self._filename = None

        self._is_video = self._check_is_video()
        self._is_tv = self._detect_tv_content()
        xbmc.log('[TheIntroDB] Playback started: {} (tv={}, video={})'.format(
            self._filename, self._is_tv, self._is_video), xbmc.LOGINFO)

    def onPlayBackStopped(self) -> None:
        self._reset()

    def onPlayBackEnded(self) -> None:
        self._reset()

    def onPlayBackError(self) -> None:
        self._reset()

    def onPlayBackPaused(self) -> None:
        self._is_paused = True
        self._pause_count += 1
        xbmc.log('[TheIntroDB] Playback paused (count={})'.format(self._pause_count), xbmc.LOGINFO)

    def onPlayBackResumed(self) -> None:
        self._is_paused = False

    def _reset(self) -> None:
        xbmc.log('[TheIntroDB] Playback ended/stopped', xbmc.LOGINFO)
        self._playback_started = False
        self._filename = None
        self._is_tv = False
        self._is_video = False
        self._is_paused = False
        self._pause_count = 0

    def _check_is_video(self) -> bool:
        try:
            return self.isPlayingVideo()
        except Exception:
            return False

    def _detect_tv_content(self) -> bool:
        # episodes, sxxeyy in path, or long video — rough filter for streamers
        if not self._is_video:
            return False

        try:
            tag = self.getVideoInfoTag()
            if tag.getSeason() > 0 and tag.getEpisode() > 0:
                return True
            media_type = tag.getMediaType()
            if media_type == 'episode':
                return True
        except Exception:
            pass

        if self._filename:
            if re.search(r'[Ss]\d{1,2}[Ee]\d{1,2}', self._filename):
                return True

        min_duration = 600
        try:
            total = self.getTotalTime()
            if total < min_duration:
                return False
        except Exception:
            pass

        return True

    def get_media_ids(self) -> Dict[str, Any]:
        # library sources first (json-rpc uniqueid, videoinfotag), then what
        # TMDbHelper publishes about the item it launched
        ids: Dict[str, Any] = {
            'imdb_id': None,
            'tmdb_id': None,
            'tvdb_id': None,
            'tvdb_episode_id': None,
            'season': None,
            'episode': None,
            'is_movie': False,
            'duration_ms': None,
            'year': None,
        }

        self._ids_from_jsonrpc(ids)
        self._ids_from_infotag(ids)
        if not (ids.get('tmdb_id') or ids.get('tvdb_id') or ids.get('imdb_id')):
            self._ids_from_tmdbhelper(ids)
        if not (ids.get('tmdb_id') or ids.get('tvdb_id') or ids.get('imdb_id')):
            self._ids_from_playerinfostring(ids)
        ids['duration_ms'] = self._get_video_duration_ms()

        # Which id lands on an episode depends on the scraper: some store the
        # episode record, others repeat the series id. Only a distinct one is a
        # useful pin — sent as tvdb_id the API would look for a series by it.
        if ids.get('tvdb_episode_id') and ids['tvdb_episode_id'] == ids.get('tvdb_id'):
            ids['tvdb_episode_id'] = None

        xbmc.log('[TheIntroDB] Extracted media IDs: {}'.format(ids), xbmc.LOGINFO)
        return ids

    def _get_video_duration_ms(self) -> Optional[int]:
        try:
            tag = self.getVideoInfoTag()
            dur = tag.getDuration()
            if dur:
                dur_sec = int(dur)
                if dur_sec > 0:
                    return dur_sec * 1000
        except Exception:
            pass

        try:
            response = self._jsonrpc('Player.GetProperties', {
                'playerid': self._active_video_player_id(),
                'properties': ['totaltime'],
            })
            totaltime = (response or {}).get('result', {}).get('totaltime') or {}
            h = int(totaltime.get('hours', 0) or 0)
            m = int(totaltime.get('minutes', 0) or 0)
            s = int(totaltime.get('seconds', 0) or 0)
            ms = int(totaltime.get('milliseconds', 0) or 0)
            total_ms = ((h * 3600 + m * 60 + s) * 1000) + ms
            if total_ms > 0:
                return total_ms
        except Exception:
            pass

        try:
            total = self.getTotalTime()
            if total:
                total_ms = int(round(float(total) * 1000.0))
                if total_ms > 0:
                    return total_ms
        except Exception:
            pass

        return None

    def get_next_episode(self) -> Optional[Dict[str, Any]]:
        item = self._get_current_player_item()
        if not item or item.get('type') != 'episode':
            return None

        try:
            tvshowid = int(item.get('tvshowid'))
            current_season = int(item.get('season'))
            current_episode = int(item.get('episode'))
        except (TypeError, ValueError):
            return None

        response = self._jsonrpc('VideoLibrary.GetEpisodes', {
            'tvshowid': tvshowid,
            'properties': ['season', 'episode', 'title'],
            'sort': {'method': 'episode', 'order': 'ascending'},
        })
        episodes = (response or {}).get('result', {}).get('episodes') or []
        current_key = (current_season, current_episode)
        next_episode: Optional[Dict[str, Any]] = None
        next_key: Optional[Tuple[int, int]] = None

        for episode in episodes:
            episodeid = episode.get('episodeid')
            try:
                season = int(episode.get('season'))
                number = int(episode.get('episode'))
            except (TypeError, ValueError):
                continue
            if not episodeid:
                continue

            episode_key = (season, number)
            if episode_key <= current_key:
                continue
            if next_key is None or episode_key < next_key:
                next_key = episode_key
                next_episode = {
                    'episodeid': int(episodeid),
                    'season': season,
                    'episode': number,
                    'title': episode.get('title') or '',
                }

        if next_episode and addon_settings.setting_bool('debug_logging'):
            xbmc.log(
                '[TheIntroDB] Next episode found: S{}E{} {}'.format(
                    next_episode['season'],
                    next_episode['episode'],
                    next_episode['title'],
                ),
                xbmc.LOGINFO,
            )

        return next_episode

    def play_next_episode(self, next_episode: Optional[Dict[str, Any]] = None) -> bool:
        if next_episode is None:
            next_episode = self.get_next_episode()
        if not next_episode:
            return False

        response = self._jsonrpc('Player.Open', {
            'item': {'episodeid': next_episode['episodeid']},
        })
        if response and 'error' not in response:
            return True

        if addon_settings.setting_bool('debug_logging'):
            xbmc.log('[TheIntroDB] Failed to open next episode: {}'.format(response), xbmc.LOGWARNING)
        return False

    def _active_video_player_id(self) -> int:
        try:
            r = json.loads(xbmc.executeJSONRPC(
                '{"jsonrpc":"2.0","method":"Player.GetActivePlayers","id":1}'))
            for p in r.get('result') or []:
                if p.get('type') == 'video':
                    return int(p.get('playerid', 0))
            res = r.get('result') or []
            if res:
                return int(res[0].get('playerid', 1))
        except Exception:
            pass
        return 1

    def _get_current_player_item(self) -> Dict[str, Any]:
        response = self._jsonrpc('Player.GetItem', {
            'playerid': self._active_video_player_id(),
            'properties': [
                'id', 'tvshowid', 'season', 'episode', 'showtitle', 'title', 'type',
            ],
        })
        return (response or {}).get('result', {}).get('item') or {}

    def _jsonrpc(self, method: str, params: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
        try:
            payload: Dict[str, Any] = {'jsonrpc': '2.0', 'method': method, 'id': 1}
            if params is not None:
                payload['params'] = params
            return json.loads(xbmc.executeJSONRPC(json.dumps(payload)))
        except Exception:
            return None

    def _extract_numeric_id(self, value: Any) -> Optional[str]:
        try:
            s = str(value).strip()
            if not s:
                return None
            if int(s) > 0:
                return s
        except (ValueError, TypeError):
            return None
        return None

    def _extract_tmdb_from_uniqueid(self, unique: Any, keys: Tuple[str, ...]) -> Optional[str]:
        if not isinstance(unique, dict):
            return None
        for key in keys:
            val = self._extract_numeric_id(unique.get(key))
            if val:
                return val
        return None

    def _apply_uniqueid_dict(self, unique: Any, ids: Dict[str, Any]) -> None:
        if not isinstance(unique, dict):
            return

        if not ids.get('tmdb_id'):
            tmdb_val = unique.get('tmdb') or unique.get('themoviedb')
            if tmdb_val:
                try:
                    if int(tmdb_val) > 0:
                        ids['tmdb_id'] = str(tmdb_val)
                except (ValueError, TypeError):
                    pass

        if not ids.get('imdb_id'):
            imdb_val = unique.get('imdb')
            if imdb_val and str(imdb_val).startswith('tt'):
                ids['imdb_id'] = str(imdb_val)

        if not ids.get('tvdb_id'):
            tvdb_val = unique.get('tvdb') or unique.get('thetvdb')
            if tvdb_val:
                try:
                    if int(tvdb_val) > 0:
                        ids['tvdb_id'] = str(tvdb_val)
                except (ValueError, TypeError):
                    pass

        if not ids.get('tmdb_id'):
            tmdb_show = unique.get('tmdbshow') or unique.get('tmdb_show')
            if tmdb_show:
                try:
                    if int(tmdb_show) > 0:
                        ids['tmdb_id'] = str(tmdb_show)
                except (ValueError, TypeError):
                    pass

    def _apply_episode_item_uniqueid_dict(self, unique: Any, ids: Dict[str, Any]) -> None:
        if not isinstance(unique, dict):
            return

        if not ids.get('tmdb_id'):
            tmdb_show = unique.get('tmdbshow') or unique.get('tmdb_show')
            if tmdb_show:
                try:
                    if int(tmdb_show) > 0:
                        ids['tmdb_id'] = str(tmdb_show)
                except (ValueError, TypeError):
                    pass

        if not ids.get('imdb_id'):
            imdb_val = unique.get('imdb')
            if imdb_val and str(imdb_val).startswith('tt'):
                ids['imdb_id'] = str(imdb_val)

        # On an episode, the TVDB scraper's `tvdb` uniqueid is the episode record
        # (the series id arrives with the show's own uniqueid below).
        if not ids.get('tvdb_episode_id'):
            tvdb_val = unique.get('tvdb')
            if tvdb_val:
                try:
                    if int(tvdb_val) > 0:
                        ids['tvdb_episode_id'] = str(tvdb_val)
                except (ValueError, TypeError):
                    pass

        if not ids.get('tvdb_id'):
            tvdb_show = unique.get('tvdbshow') or unique.get('tvdb_show')
            if tvdb_show:
                try:
                    if int(tvdb_show) > 0:
                        ids['tvdb_id'] = str(tvdb_show)
                except (ValueError, TypeError):
                    pass

    def _tvshow_ids_from_library(self, tvshowid: Any, ids: Dict[str, Any], force_tmdb: bool = False) -> None:
        try:
            tvshowid_int = int(tvshowid)
        except (ValueError, TypeError):
            return

        r = self._jsonrpc('VideoLibrary.GetTVShowDetails', {
            'tvshowid': tvshowid_int,
            'properties': ['uniqueid', 'imdbnumber', 'title', 'year'],
        })
        details = (r or {}).get('result', {}).get('tvshowdetails') or {}
        unique = details.get('uniqueid')
        show_tmdb = self._extract_tmdb_from_uniqueid(unique, ('tmdb', 'themoviedb', 'tmdbshow', 'tmdb_show'))
        if show_tmdb and (force_tmdb or not ids.get('tmdb_id')):
            old = ids.get('tmdb_id')
            ids['tmdb_id'] = show_tmdb
            if addon_settings.setting_bool('debug_logging'):
                xbmc.log('[TheIntroDB] TV show TMDB resolved: {} -> {} (uniqueid={})'.format(
                    old or '-', show_tmdb, unique), xbmc.LOGINFO)

        if not ids.get('imdb_id'):
            imdb_unique = (unique or {}).get('imdb') if isinstance(unique, dict) else None
            if imdb_unique and str(imdb_unique).startswith('tt'):
                ids['imdb_id'] = str(imdb_unique)

        if not ids.get('tvdb_id'):
            tvdb_unique = (unique or {}).get('tvdb') if isinstance(unique, dict) else None
            if tvdb_unique:
                try:
                    if int(tvdb_unique) > 0:
                        ids['tvdb_id'] = str(tvdb_unique)
                except (ValueError, TypeError):
                    pass

        imdbnumber = details.get('imdbnumber')
        if imdbnumber and str(imdbnumber).startswith('tt') and not ids.get('imdb_id'):
            ids['imdb_id'] = str(imdbnumber)

    def _tvshow_ids_by_title(self, title: Optional[str], ids: Dict[str, Any], force_tmdb: bool = False) -> None:
        if not title:
            return

        r = self._jsonrpc('VideoLibrary.GetTVShows', {
            'filter': {'field': 'title', 'operator': 'is', 'value': str(title)},
            'properties': ['title', 'uniqueid', 'imdbnumber', 'year'],
        })
        tvshows = (r or {}).get('result', {}).get('tvshows') or []
        if not tvshows:
            return

        pick = None
        for t in tvshows:
            if str(t.get('title') or '').strip().lower() == str(title).strip().lower():
                pick = t
                break
        if pick is None:
            pick = tvshows[0]

        unique = pick.get('uniqueid')
        show_tmdb = self._extract_tmdb_from_uniqueid(unique, ('tmdb', 'themoviedb', 'tmdbshow', 'tmdb_show'))
        if show_tmdb and (force_tmdb or not ids.get('tmdb_id')):
            old = ids.get('tmdb_id')
            ids['tmdb_id'] = show_tmdb
            if addon_settings.setting_bool('debug_logging'):
                xbmc.log('[TheIntroDB] TV show TMDB resolved (title match): {} -> {} (title={} uniqueid={})'.format(
                    old or '-', show_tmdb, title, unique), xbmc.LOGINFO)

        if not ids.get('imdb_id'):
            imdb_unique = (unique or {}).get('imdb') if isinstance(unique, dict) else None
            if imdb_unique and str(imdb_unique).startswith('tt'):
                ids['imdb_id'] = str(imdb_unique)

        if not ids.get('tvdb_id'):
            tvdb_unique = (unique or {}).get('tvdb') if isinstance(unique, dict) else None
            if tvdb_unique:
                try:
                    if int(tvdb_unique) > 0:
                        ids['tvdb_id'] = str(tvdb_unique)
                except (ValueError, TypeError):
                    pass

        imdbnumber = pick.get('imdbnumber')
        if imdbnumber and str(imdbnumber).startswith('tt') and not ids.get('imdb_id'):
            ids['imdb_id'] = str(imdbnumber)

    def _ids_from_jsonrpc(self, ids: Dict[str, Any]) -> None:
        try:
            pid = self._active_video_player_id()
            response = self._jsonrpc('Player.GetItem', {
                'playerid': pid,
                'properties': [
                    'id', 'tvshowid', 'uniqueid', 'imdbnumber', 'season', 'episode',
                    'showtitle', 'title', 'type',
                ],
            })
            item = (response or {}).get('result', {}).get('item') or {}

            if item.get('year'):
                ids['year'] = try_int(item['year'])

            if addon_settings.setting_bool('debug_logging'):
                xbmc.log('[TheIntroDB] JSON-RPC item: type={} uniqueid={} imdbnumber={}'.format(
                    item.get('type'), item.get('uniqueid'), item.get('imdbnumber')),
                    xbmc.LOGINFO)

            imdbnumber = item.get('imdbnumber')
            if imdbnumber:
                if str(imdbnumber).startswith('tt') and not ids['imdb_id']:
                    ids['imdb_id'] = str(imdbnumber)

            if item.get('season') and item['season'] > 0:
                ids['season'] = item['season']
            if item.get('episode') and item['episode'] > 0:
                ids['episode'] = item['episode']

            item_type = item.get('type', '')
            if item_type == 'movie':
                ids['is_movie'] = True
                self._apply_uniqueid_dict(item.get('uniqueid'), ids)
            elif item_type == 'episode':
                ids['is_movie'] = False
                self._apply_episode_item_uniqueid_dict(item.get('uniqueid'), ids)
                self._tvshow_ids_from_library(item.get('tvshowid'), ids, force_tmdb=True)
                self._tvshow_ids_by_title(item.get('showtitle'), ids, force_tmdb=True)

        except Exception as e:
            xbmc.log('[TheIntroDB] JSON-RPC Player.GetItem failed: {}'.format(e),
                      xbmc.LOGWARNING)

    def _ids_from_infotag(self, ids: Dict[str, Any]) -> None:
        try:
            tag = self.getVideoInfoTag()
        except Exception:
            return

        if not ids['imdb_id']:
            try:
                imdb = tag.getIMDBNumber()
                if imdb and str(imdb).startswith('tt'):
                    ids['imdb_id'] = str(imdb)
            except Exception:
                pass

        # TVDB is the API's identity, so the id is worth collecting even when a
        # TMDB one exists. On an episode tag `tvdb` is the episode record and the
        # series id lives on the show (tvdb_show / tvdbshow) — the two are told
        # apart by the media type, and get_media_ids drops the episode id when a
        # scraper repeated the series id on it.
        try:
            media_type = tag.getMediaType()
        except Exception:
            media_type = None

        try:
            tvdb = tag.getUniqueID('tvdb')
            if tvdb and not str(tvdb).startswith('tt'):
                val = str(tvdb)
                if int(val) > 0:
                    if media_type == 'movie':
                        if not ids.get('tvdb_id'):
                            ids['tvdb_id'] = val
                    elif not ids.get('tvdb_episode_id'):
                        ids['tvdb_episode_id'] = val
        except Exception:
            pass

        if not ids.get('tvdb_id'):
            for key in ('tvdb_show', 'tvdbshow'):
                try:
                    tvdb_show = tag.getUniqueID(key)
                except Exception:
                    tvdb_show = None
                if not tvdb_show:
                    continue
                try:
                    if int(str(tvdb_show)) > 0:
                        ids['tvdb_id'] = str(tvdb_show)
                        break
                except (ValueError, TypeError):
                    continue

        if not ids['tmdb_id']:
            try:
                for key in ('tmdbshow', 'tmdb_show', 'themoviedb', 'tmdb'):
                    tmdb = tag.getUniqueID(key)
                    if not tmdb:
                        continue
                    val = str(tmdb)
                    if not val or val.startswith('tt'):
                        continue
                    if key in ('tmdb', 'themoviedb') and (ids.get('season') and ids.get('episode') and not ids.get('is_movie')):
                        continue
                    try:
                        if int(val) > 0:
                            ids['tmdb_id'] = val
                            break
                    except (ValueError, TypeError):
                        pass
            except Exception:
                pass

        if not ids['season'] or ids['season'] <= 0:
            try:
                s = tag.getSeason()
                if s and s > 0:
                    ids['season'] = s
            except Exception:
                pass

        if not ids['episode'] or ids['episode'] <= 0:
            try:
                e = tag.getEpisode()
                if e and e > 0:
                    ids['episode'] = e
            except Exception:
                pass

        if not ids.get('year'):
            try:
                tag_year = tag.getYear()
                if tag_year and tag_year > 0:
                    ids['year'] = tag_year
            except Exception:
                pass

        try:
            media_type = tag.getMediaType()
            if media_type == 'movie':
                ids['is_movie'] = True
            elif media_type == 'episode':
                ids['is_movie'] = False
        except Exception:
            pass

        if ids.get('season') and ids.get('episode') and not ids.get('is_movie'):
            try:
                showtitle = tag.getTVShowTitle()
            except Exception:
                showtitle = None
            if showtitle:
                self._tvshow_ids_by_title(showtitle, ids, force_tmdb=True)

    def _tmdbhelper_property(self, name: str) -> Optional[str]:
        try:
            value = xbmcgui.Window(_TMDBHELPER_WINDOW).getProperty(_TMDBHELPER_PREFIX + name)
        except Exception:
            return None
        if value is None:
            return None
        value = str(value).strip()
        return value or None

    def _tmdbhelper_conflicts(self, ids: Dict[str, Any], season: Any, episode: Any, year: Any) -> bool:
        """Whether property values contradict the item we are watching.

        TMDbHelper keeps writing over the same properties, so old values may
        still be around for a moment when the next item starts. Whenever the
        property values and the item's own info are both known and disagree,
        the property describes a different item and must be ignored.
        """
        if ids.get('is_movie') and (season or episode):
            return True
        if not ids.get('is_movie') and (ids.get('season') or ids.get('episode')):
            if season and ids.get('season') and try_int(ids['season']) != try_int(season):
                return True
            if episode and ids.get('episode') and try_int(ids['episode']) != try_int(episode):
                return True
        if ids.get('season') and ids.get('episode') and not (season or episode):
            # Properties came from a season-less item — only a movie item looks
            # like this, so an episode id mix-up stays out.
            return True
        if year and try_int(year) and ids.get('year') and try_int(ids['year']) != try_int(year):
            return True
        return False

    def _ids_from_tmdbhelper(self, ids: Dict[str, Any]) -> None:
        """Take the ids TMDbHelper resolved for the item it is playing.

        Its monitor looks up the played item — TVDB and TMDB ids of the show a
        streamer episode belongs to included — and sets them on the home window
        as TMDbHelper.Player.* properties. Episode-granularity ids ('tvdb_id',
        'tmdb_id' on an episode) are kept apart: only the show-level ones
        identify a title for TheIntroDB, the episode record is passed as its own
        field so the API can use it.
        """
        season = self._tmdbhelper_property('season')
        episode = self._tmdbhelper_property('episode')
        year = self._tmdbhelper_property('year')

        if self._tmdbhelper_conflicts(ids, season, episode, year):
            if addon_settings.setting_bool('debug_logging'):
                xbmc.log('[TheIntroDB] TMDbHelper player properties belong to another item — ignored',
                         xbmc.LOGINFO)
            return

        if not ids.get('tvdb_id'):
            if ids.get('is_movie'):
                tvdb = self._extract_numeric_id(self._tmdbhelper_property('tvdb_id'))
            else:
                tvdb = self._extract_numeric_id(self._tmdbhelper_property('tvshow.tvdb_id'))
            if tvdb:
                ids['tvdb_id'] = tvdb
        if not ids.get('tvdb_episode_id') and not ids.get('is_movie'):
            tvdb_ep = self._extract_numeric_id(self._tmdbhelper_property('tvdb_id'))
            if tvdb_ep and tvdb_ep != ids.get('tvdb_id'):
                ids['tvdb_episode_id'] = tvdb_ep
        if not ids.get('tmdb_id'):
            if ids.get('is_movie'):
                tmdb = self._extract_numeric_id(self._tmdbhelper_property('tmdb_id'))
            else:
                tmdb = self._extract_numeric_id(self._tmdbhelper_property('tvshow.tmdb_id'))
            if tmdb:
                ids['tmdb_id'] = tmdb
        if not ids.get('imdb_id'):
            if ids.get('is_movie'):
                imdb = self._tmdbhelper_property('imdb_id')
            else:
                # imdb_id in this scope is the episode record; the series id lives in tvshow.imdb_id
                imdb = self._tmdbhelper_property('tvshow.imdb_id')
            if imdb and str(imdb).startswith('tt'):
                ids['imdb_id'] = str(imdb)

        if ids.get('season') is None and season:
            ids['season'] = try_int(season)
        if ids.get('episode') is None and episode:
            ids['episode'] = try_int(episode)

        if not (ids.get('tmdb_id') or ids.get('tvdb_id') or ids.get('imdb_id')):
            return

        xbmc.log('[TheIntroDB] Media ids recovered from TMDbHelper player properties', xbmc.LOGINFO)

    def _ids_from_playerinfostring(self, ids: Dict[str, Any]) -> None:
        """Reminder of the item TMDbHelper itself started playing.

        Its play flow leaves a JSON with the ids of the started item in the
        TMDbHelper.PlayerInfoString property. Only present for plays TMDbHelper
        resolved on its own (players opening a file directly, not a folder), so
        this stays a secondary source.
        """
        try:
            raw = xbmcgui.Window(_TMDBHELPER_WINDOW).getProperty('TMDbHelper.PlayerInfoString')
        except Exception:
            return
        if not raw:
            return

        try:
            payload = json.loads(raw)
        except Exception:
            return
        if not isinstance(payload, dict):
            return

        prop_movie = payload.get('tmdb_type') == 'movie'
        season = payload.get('season')
        episode = payload.get('episode')

        if self._tmdbhelper_conflicts(ids, season, episode, None):
            if addon_settings.setting_bool('debug_logging'):
                xbmc.log('[TheIntroDB] PlayerInfoString belongs to another item — ignored', xbmc.LOGINFO)
            return

        if not ids.get('tmdb_id'):
            tmdb = self._extract_numeric_id(payload.get('tmdb_id'))
            if tmdb:
                ids['tmdb_id'] = tmdb
        if not ids.get('imdb_id'):
            imdb = payload.get('imdb_id')
            if imdb and str(imdb).startswith('tt'):
                ids['imdb_id'] = str(imdb)
        if not ids.get('tvdb_id'):
            tvdb = self._extract_numeric_id(payload.get('tvdb_id'))
            if tvdb:
                ids['tvdb_id'] = tvdb

        if not prop_movie:
            if ids.get('season') is None and season:
                ids['season'] = try_int(season)
            if ids.get('episode') is None and episode:
                ids['episode'] = try_int(episode)

        if ids.get('tmdb_id') or ids.get('tvdb_id') or ids.get('imdb_id'):
            xbmc.log('[TheIntroDB] Media ids recovered from TMDbHelper PlayerInfoString', xbmc.LOGINFO)
