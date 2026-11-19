"""Channel adapters, chosen by CHANNEL=twilio|sim|console."""
from .base import ChannelRequest, InboundMessage, OutboundMessage, Option, DeliveryStatus, SendResult  # noqa: F401


def get_channel(settings, client=None):
    name = settings.channel
    if name == "twilio":
        from .twilio_wa import TwilioChannel
        return TwilioChannel(settings, client=client)
    if name == "sim":
        from .sim import SimChannel
        return SimChannel(settings)
    if name == "console":
        from .console import ConsoleChannel
        return ConsoleChannel(settings)
    raise ValueError(f"CHANNEL must be twilio, sim or console (got {name!r})")
