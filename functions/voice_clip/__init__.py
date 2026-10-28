"""Rolling voice-channel audio clip capture, mixing and authorized DM delivery."""

from .buffer import (
    BufferedAudioPacket,
    ClipIntervalSnapshot,
    RollingAudioBuffer,
    SpeakerIntervalPackets,
)
from .consent import ConsentManager, RecordingConsentView, build_recording_notice_text
from .constants import (
    AUTHORIZED_CLIP_USER_ID,
    ClipAuthorizationError,
    ClipBusyError,
    ClipDeliveryError,
    ClipError,
    ClipExportError,
    ClipExportTimeoutError,
    ClipFileTooLargeError,
    ClipUnavailableError,
    ClipValidationError,
    MAX_CLIP_DURATION,
    MIN_CLIP_DURATION,
    NoUsableAudioError,
    OPUS_SILENCE,
    SAMPLE_RATE,
    UNAUTHORIZED_CLIP_MESSAGE,
    authorize_clip_user,
    require_clip_authorization,
    validate_clip_duration,
)
from .exporter import ClipExportPipeline, ExportedClip, build_clip_filename
from .mixer import MixResult, apply_smooth_peak_limiter, mix_snapshot_to_pcm_f32
from .opus_codec import SpeakerOpusDecoder, ensure_opus_loaded
from .receiver import (
    ClipVoiceClient,
    ParsedRTPPacket,
    TransportDecryptor,
    VoiceReceiveRouter,
)
from .service import ClipDeliveryReport, VoiceClipService, format_clip_dm_text

__all__ = [
    "AUTHORIZED_CLIP_USER_ID",
    "BufferedAudioPacket",
    "ClipAuthorizationError",
    "ClipBusyError",
    "ClipDeliveryError",
    "ClipDeliveryReport",
    "ClipError",
    "ClipExportError",
    "ClipExportPipeline",
    "ClipExportTimeoutError",
    "ClipFileTooLargeError",
    "ClipIntervalSnapshot",
    "ClipUnavailableError",
    "ClipValidationError",
    "ClipVoiceClient",
    "ConsentManager",
    "ExportedClip",
    "MAX_CLIP_DURATION",
    "MIN_CLIP_DURATION",
    "MixResult",
    "NoUsableAudioError",
    "OPUS_SILENCE",
    "ParsedRTPPacket",
    "RecordingConsentView",
    "RollingAudioBuffer",
    "SAMPLE_RATE",
    "SpeakerIntervalPackets",
    "SpeakerOpusDecoder",
    "TransportDecryptor",
    "UNAUTHORIZED_CLIP_MESSAGE",
    "VoiceClipService",
    "VoiceReceiveRouter",
    "apply_smooth_peak_limiter",
    "authorize_clip_user",
    "build_clip_filename",
    "build_recording_notice_text",
    "ensure_opus_loaded",
    "format_clip_dm_text",
    "mix_snapshot_to_pcm_f32",
    "require_clip_authorization",
    "validate_clip_duration",
]
