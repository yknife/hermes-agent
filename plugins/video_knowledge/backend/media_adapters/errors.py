class MediaToolError(Exception):
    code = "TOOL_CRASH"
    retryable = True


class UnsupportedUrlError(MediaToolError):
    code = "UNSUPPORTED_URL"
    retryable = False


class AuthenticationRequiredError(MediaToolError):
    code = "AUTH_REQUIRED"
    retryable = False


class RateLimitedError(MediaToolError):
    code = "RATE_LIMITED"


class NetworkTimeoutError(MediaToolError):
    code = "NETWORK_TIMEOUT"


class InvalidMediaError(MediaToolError):
    code = "INVALID_MEDIA"
    retryable = False


class SubtitleNotFoundError(MediaToolError):
    code = "SUBTITLE_NOT_FOUND"
    retryable = False


class SubtitleParseError(MediaToolError):
    code = "SUBTITLE_PARSE_ERROR"
    retryable = False
