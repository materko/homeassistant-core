"""Reolink additional services."""

from datetime import datetime, timedelta
from urllib.parse import quote

from reolink_aio.api import Chime
from reolink_aio.enums import ChimeToneEnum, VodRequestType
from reolink_aio.typings import VOD_file
from reolink_aio.utils import to_reolink_time_id
import voluptuous as vol

from homeassistant.components.button import DOMAIN as BUTTON_DOMAIN
from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.const import ATTR_DEVICE_ID
from homeassistant.core import (
    HomeAssistant,
    ServiceCall,
    ServiceResponse,
    SupportsResponse,
    callback,
)
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import (
    config_validation as cv,
    device_registry as dr,
    service,
)
from homeassistant.util import dt as dt_util, slugify

from .const import DOMAIN, SUPPORT_PTZ_SPEED
from .host import ReolinkHost
from .util import (
    get_device_uid_and_ch,
    get_seek,
    get_vod_type,
    raise_translated_error,
)

ATTR_RINGTONE = "ringtone"
ATTR_SPEED = "speed"
SERVICE_PTZ_MOVE = "ptz_move"

ATTR_TIMESTAMP = "timestamp"
ATTR_PRE_ROLL = "pre_roll"
ATTR_STREAM = "stream"
ATTR_DURATION = "duration"
ATTR_FILENAME = "filename"
SERVICE_VOD_LINK = "vod_link"
SERVICE_VOD_DOWNLOAD = "vod_download"

# How far back to look for the recording covering a moment. Continuous recording is
# stored in blocks of roughly an hour, so a few hours of margin finds the block
# without dragging in a needlessly large search.
VOD_SEARCH_MARGIN = timedelta(hours=4)

# A recorder only indexes the block it is currently writing every few minutes, so a
# moment near the live edge reads as being past the end of a block that in fact
# already holds the footage. Accept a block that ends this recently before the moment.
VOD_INDEX_LAG = timedelta(minutes=15)


@raise_translated_error
async def _async_play_chime(service_call: ServiceCall) -> None:
    """Play a ringtone."""
    service_data = service_call.data
    device_registry = dr.async_get(service_call.hass)

    for device_id in service_data[ATTR_DEVICE_ID]:
        config_entry = None
        device = device_registry.async_get(device_id)
        if device is not None:
            for entry_id in device.config_entries:
                config_entry = service_call.hass.config_entries.async_get_entry(
                    entry_id
                )
                if config_entry is not None and config_entry.domain == DOMAIN:
                    break
        if (
            config_entry is None
            or device is None
            or config_entry.state is not ConfigEntryState.LOADED
        ):
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="service_entry_ex",
                translation_placeholders={"service_name": "play_chime"},
            )
        host: ReolinkHost = config_entry.runtime_data.host
        (_device_uid, chime_id, is_chime) = get_device_uid_and_ch(device, host)
        chime: Chime | None = host.api.chime(chime_id)
        if not is_chime or chime is None:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="service_not_chime",
                translation_placeholders={"device_name": str(device.name)},
            )

        ringtone = service_data[ATTR_RINGTONE]
        await chime.play(ChimeToneEnum[ringtone].value)


def _get_device_entry(
    service_call: ServiceCall, device_id: str, service_name: str
) -> tuple[dr.DeviceEntry, ConfigEntry]:
    """Return the device and its loaded Reolink config entry."""
    device = dr.async_get(service_call.hass).async_get(device_id)
    config_entry = None
    if device is not None:
        for entry_id in device.config_entries:
            entry = service_call.hass.config_entries.async_get_entry(entry_id)
            if entry is not None and entry.domain == DOMAIN:
                config_entry = entry
                break

    if (
        device is None
        or config_entry is None
        or config_entry.state is not ConfigEntryState.LOADED
    ):
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="service_entry_ex",
            translation_placeholders={"service_name": service_name},
        )

    return device, config_entry


async def _async_locate_recording(
    service_call: ServiceCall, service_name: str
) -> tuple[dr.DeviceEntry, ConfigEntry, ReolinkHost, int, datetime, VOD_file]:
    """Find the recording covering the requested moment.

    The recorder holds hours of continuous video, so reaching the footage behind an
    alarm means locating the block the moment falls in.
    """
    service_data = service_call.data
    device, config_entry = _get_device_entry(
        service_call, service_data[ATTR_DEVICE_ID], service_name
    )

    host: ReolinkHost = config_entry.runtime_data.host
    (_device_uid, channel, is_chime) = get_device_uid_and_ch(device, host)
    if is_chime or channel is None:
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="service_not_camera",
            translation_placeholders={"device_name": str(device.name)},
        )

    stream = service_data[ATTR_STREAM]
    timestamp = service_data.get(ATTR_TIMESTAMP) or dt_util.now()
    moment = dt_util.as_local(timestamp) - timedelta(
        seconds=service_data[ATTR_PRE_ROLL]
    )

    # A search may not span two calendar months on every firmware, so never reach
    # back past the start of the month the moment falls in.
    month_start = moment.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    search_start = max(moment - VOD_SEARCH_MARGIN, month_start)

    _statuses, files = await host.api.request_vod_files(
        channel, search_start, moment + timedelta(seconds=1), stream=stream
    )

    # The block covering the moment is the latest one that started before it, whether
    # the recorder has caught up with indexing its end or not.
    recording = max(
        (
            file
            for file in files or []
            if file.start_time <= moment <= file.end_time + VOD_INDEX_LAG
        ),
        key=lambda file: file.start_time,
        default=None,
    )
    if recording is None:
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="service_no_recording",
            translation_placeholders={
                "device_name": str(device.name),
                "moment": moment.isoformat(timespec="seconds"),
            },
        )

    return device, config_entry, host, channel, moment, recording


@raise_translated_error
async def _async_vod_link(service_call: ServiceCall) -> ServiceResponse:
    """Return a link that plays the recording covering a given moment."""
    _device, config_entry, _host, channel, moment, recording = (
        await _async_locate_recording(service_call, SERVICE_VOD_LINK)
    )
    stream = service_call.data[ATTR_STREAM]

    # The media source derives the seek offset from the difference between this start
    # time and the start of the block, so naming the moment here starts playback there.
    identifier = (
        f"FILE|{config_entry.entry_id}|{channel}|{stream}"
        f"|{recording.file_name}|{to_reolink_time_id(moment)}"
        f"|{recording.end_time_id}"
    )
    media_content_id = f"media-source://{DOMAIN}/{identifier}"

    return {
        "media_content_id": media_content_id,
        # Ready to hand to a navigate action or a markdown link.
        "path": f"/media-browser/browser/{quote(f'video,{media_content_id}', safe='')}",
        "start": moment.isoformat(timespec="seconds"),
        "recording_start": recording.start_time.isoformat(timespec="seconds"),
    }


@raise_translated_error
async def _async_vod_download(service_call: ServiceCall) -> ServiceResponse:
    """Copy the recording covering a given moment into Home Assistant.

    Pulls the footage straight off the recorder rather than off the live stream, so
    the clip can start before the moment that triggered it without Home Assistant
    having to keep a preloaded stream running.
    """
    device, _config_entry, host, channel, moment, recording = (
        await _async_locate_recording(service_call, SERVICE_VOD_DOWNLOAD)
    )
    service_data = service_call.data
    stream_res = service_data[ATTR_STREAM]
    duration = service_data[ATTR_DURATION]

    video_path = service_data.get(ATTR_FILENAME) or (
        f"/media/reolink/{slugify(str(device.name))}"
        f"/{moment.strftime('%Y-%m-%d_%H-%M-%S')}.mp4"
    )

    filename = recording.file_name
    vod_type = get_vod_type(host, filename)
    seek = get_seek(filename, to_reolink_time_id(moment))
    if vod_type is VodRequestType.NVR_DOWNLOAD:
        # This request type addresses a recording by the span it covers, not by name.
        filename = f"{recording.start_time_id}_{recording.end_time_id}"

    _mime_type, url = await host.api.get_vod_source(
        channel, filename, stream_res, vod_type, seek
    )

    # Imported here so that setting up the integration does not pull in the stream
    # stack, which only this service needs.
    from homeassistant.components.camera import (  # noqa: PLC0415
        DynamicStreamSettings,
    )
    from homeassistant.components.stream import create_stream  # noqa: PLC0415

    stream = create_stream(service_call.hass, url, {}, DynamicStreamSettings())
    try:
        await stream.async_record(video_path, duration=duration, lookback=0)
    finally:
        # Nothing else consumes this stream, so let go of the recorder connection
        # instead of waiting for it to time out.
        await stream.stop()

    return {
        "filename": video_path,
        "start": moment.isoformat(timespec="seconds"),
        "duration": duration,
        "recording_start": recording.start_time.isoformat(timespec="seconds"),
    }


@callback
def async_setup_services(hass: HomeAssistant) -> None:
    """Set up Reolink services."""

    hass.services.async_register(
        DOMAIN,
        "play_chime",
        _async_play_chime,
        schema=vol.Schema(
            {
                vol.Required(ATTR_DEVICE_ID): list[str],
                vol.Required(ATTR_RINGTONE): vol.In(
                    [method.name for method in ChimeToneEnum][1:]
                ),
            }
        ),
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_VOD_LINK,
        _async_vod_link,
        schema=vol.Schema(
            {
                vol.Required(ATTR_DEVICE_ID): cv.string,
                vol.Optional(ATTR_TIMESTAMP): cv.datetime,
                vol.Optional(ATTR_PRE_ROLL, default=0): cv.positive_int,
                vol.Optional(ATTR_STREAM, default="sub"): vol.In(["sub", "main"]),
            }
        ),
        supports_response=SupportsResponse.ONLY,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_VOD_DOWNLOAD,
        _async_vod_download,
        schema=vol.Schema(
            {
                vol.Required(ATTR_DEVICE_ID): cv.string,
                vol.Optional(ATTR_TIMESTAMP): cv.datetime,
                vol.Optional(ATTR_PRE_ROLL, default=0): cv.positive_int,
                vol.Optional(ATTR_DURATION, default=30): vol.All(
                    cv.positive_int, vol.Range(min=1, max=600)
                ),
                vol.Optional(ATTR_FILENAME): cv.string,
                vol.Optional(ATTR_STREAM, default="sub"): vol.In(["sub", "main"]),
            }
        ),
        supports_response=SupportsResponse.OPTIONAL,
    )
    service.async_register_platform_entity_service(
        hass,
        DOMAIN,
        SERVICE_PTZ_MOVE,
        entity_domain=BUTTON_DOMAIN,
        schema={vol.Required(ATTR_SPEED): cv.positive_int},
        func="async_ptz_move",
        required_features=[SUPPORT_PTZ_SPEED],
    )
