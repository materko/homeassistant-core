"""Reolink additional services."""

from datetime import datetime, timedelta
from urllib.parse import quote

from reolink_aio.api import Chime
from reolink_aio.enums import ChimeToneEnum
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
from homeassistant.util import dt as dt_util

from .const import DOMAIN, SUPPORT_PTZ_SPEED
from .host import ReolinkHost
from .util import get_device_uid_and_ch, raise_translated_error

ATTR_RINGTONE = "ringtone"
ATTR_SPEED = "speed"
SERVICE_PTZ_MOVE = "ptz_move"

ATTR_TIMESTAMP = "timestamp"
ATTR_PRE_ROLL = "pre_roll"
ATTR_STREAM = "stream"
SERVICE_VOD_LINK = "vod_link"

# How far back to look for the recording covering a moment. Continuous recording is
# stored in blocks of roughly an hour, so a few hours of margin finds the block
# without dragging in a needlessly large search.
VOD_SEARCH_MARGIN = timedelta(hours=4)


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


@raise_translated_error
async def _async_vod_link(service_call: ServiceCall) -> ServiceResponse:
    """Return a link that plays the recording covering a given moment.

    Built for reaching the footage behind an alarm: the recorder holds hours of
    continuous video, and this locates the block covering the moment and hands back
    an identifier that starts playback there rather than at the top of the block.
    """
    service_data = service_call.data
    device, config_entry = _get_device_entry(
        service_call, service_data[ATTR_DEVICE_ID], SERVICE_VOD_LINK
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

    recording = next(
        (
            file
            for file in files or []
            if file.start_time <= moment <= file.end_time
        ),
        None,
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
    service.async_register_platform_entity_service(
        hass,
        DOMAIN,
        SERVICE_PTZ_MOVE,
        entity_domain=BUTTON_DOMAIN,
        schema={vol.Required(ATTR_SPEED): cv.positive_int},
        func="async_ptz_move",
        required_features=[SUPPORT_PTZ_SPEED],
    )
