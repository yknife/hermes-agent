from typing import Any


class DomainError(Exception):
    code = "DOMAIN_ERROR"

    def __init__(self, message: str, *, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.details = details or {}


class JobNotFoundError(DomainError):
    code = "JOB_NOT_FOUND"


class JobInvalidTransitionError(DomainError):
    code = "JOB_INVALID_TRANSITION"


class JobLeaseLostError(DomainError):
    code = "JOB_LEASE_LOST"


class JobProgressError(DomainError):
    code = "JOB_PROGRESS_INVALID"


class SourceNotFoundError(DomainError):
    code = "SOURCE_NOT_FOUND"


class MediaNotFoundError(DomainError):
    code = "MEDIA_NOT_FOUND"


class MediaDeleteConflictError(DomainError):
    code = "MEDIA_DELETE_CONFLICT"


class MediaDeleteStorageError(DomainError):
    code = "MEDIA_DELETE_STORAGE_ERROR"


class InvalidSourceUrlError(DomainError):
    code = "INVALID_SOURCE_URL"


class InvalidLocalMediaError(DomainError):
    code = "INVALID_LOCAL_MEDIA"
    retryable = False


class InvalidCookieFileError(DomainError):
    code = "INVALID_COOKIE_FILE"
    retryable = False


class InvalidStoragePathError(DomainError):
    code = "INVALID_STORAGE_PATH"
    retryable = False


class StorageMigrationConflictError(DomainError):
    code = "STORAGE_MIGRATION_CONFLICT"
    retryable = False


class TranscriptNotFoundError(DomainError):
    code = "TRANSCRIPT_NOT_FOUND"


class KnowledgeNotFoundError(DomainError):
    code = "KNOWLEDGE_NOT_FOUND"


class HermesUnavailableError(DomainError):
    code = "HERMES_UNAVAILABLE"
    retryable = True


class HermesInvalidResponseError(DomainError):
    code = "HERMES_INVALID_RESPONSE"
    retryable = False
