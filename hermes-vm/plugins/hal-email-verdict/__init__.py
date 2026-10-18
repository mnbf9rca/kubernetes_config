"""Hal-only outbound verdicts through Hermes' native email adapter."""
import json
from pathlib import Path

from gateway.config import Platform, load_gateway_config
from hermes_constants import get_hermes_home
from plugins.platforms.email import adapter as native
from tools.send_message_targets import _PLATFORM_PARSERS

NAME = 'hal_email_verdict'
HAL_HOME = Path('/home/hermes/.hermes/profiles/hal')
SCHEMA = 'hal.email-verdict.v1'


def require_native():
    if Path(get_hermes_home()).resolve() != HAL_HOME.resolve():
        raise RuntimeError('hal_verdict_requires_hal_profile')
    for method in ('_new_reply', '_smtp_send', '_send_email', '_connect_smtp', 'send'):
        if not callable(getattr(native.EmailAdapter, method, None)):
            raise RuntimeError('hal_verdict_native_adapter_incompatible')


def parse_envelope(message):
    def pairs(rows):
        result = {}
        for key, value in rows:
            if key in result: raise ValueError('hal_verdict_duplicate_field')
            result[key] = value
        return result
    if type(message) is not str:
        raise ValueError('hal_verdict_envelope_invalid')
    try: envelope = json.loads(message, object_pairs_hook=pairs)
    except (ValueError, TypeError): raise ValueError('hal_verdict_envelope_invalid') from None
    if (type(envelope) is not dict or set(envelope) != {'schema', 'subject', 'body'}
            or envelope['schema'] != SCHEMA):
        raise ValueError('hal_verdict_envelope_invalid')
    subject, body = envelope['subject'], envelope['body']
    if (type(subject) is not str or not subject.startswith('House: ')
            or not 7 < len(subject) <= 200 or subject != subject.strip()
            or any(ord(c) < 32 or ord(c) == 127 for c in subject)):
        raise ValueError('hal_verdict_subject_invalid')
    if (type(body) is not str or not body
            or any(marker in body for marker in ('\0', 'MEDIA:', '[[as_document]]'))):
        raise ValueError('hal_verdict_body_invalid')
    return subject, body


class VerdictAdapter(native.EmailAdapter):
    def __init__(self, config):
        require_native()
        email_config = load_gateway_config().platforms.get(Platform.EMAIL)
        if email_config is None or not email_config.enabled:
            raise RuntimeError('hal_verdict_native_email_not_configured')
        super().__init__(email_config)
        self.platform = Platform(NAME)

    async def connect(self, *, is_reconnect=False):
        require_native()
        self._running = True
        return True

    async def disconnect(self):
        self._running = False

    def _new_reply(self, to_addr, body, reply_to_msg_id=None, *, attach_empty_body=False):
        subject, body = parse_envelope(body)
        message, message_id, _ = super()._new_reply(to_addr, body, reply_to_msg_id,
                                                    attach_empty_body=attach_empty_body)
        message.replace_header('Subject', subject)
        # Per-message state keeps concurrent native executor sends independent.
        message._hal_verdict_subject = subject
        return message, message_id, subject

    def _smtp_send(self, message):
        require_native()
        expected = getattr(message, '_hal_verdict_subject', None)
        if expected is None or message.get_all('Subject', []) != [expected]:
            raise ValueError('hal_verdict_subject_binding_missing')
        return super()._smtp_send(message)


async def standalone_send(config, chat_id, message, *, thread_id=None, media_files=None, force_document=False):
    require_native()
    if thread_id or media_files or force_document:
        raise ValueError('hal_verdict_text_only')
    parse_envelope(message)
    result = await VerdictAdapter(config).send(chat_id, message)
    if result.success: return {'success': True, 'platform': NAME, 'chat_id': chat_id, 'message_id': result.message_id}
    return {'error': result.error or 'hal_verdict_native_send_failed'}


def register(ctx):
    require_native()
    parser = _PLATFORM_PARSERS.get('email')
    if not callable(parser): raise RuntimeError('hal_verdict_native_target_parser_incompatible')
    ctx.register_platform(name=NAME, label='Hal verdict email', adapter_factory=VerdictAdapter,
        check_fn=lambda: True, standalone_sender_fn=standalone_send, parse_target_ref_fn=parser,
        max_message_length=0, pii_safe=True, allow_update_command=False)
