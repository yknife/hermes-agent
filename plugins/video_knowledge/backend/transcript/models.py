from dataclasses import dataclass


@dataclass(frozen=True)
class TranscriptSegment:
    index: int
    start_ms: int
    end_ms: int
    text: str
    speaker: str | None = None
    confidence: float | None = None


@dataclass(frozen=True)
class NormalizedTranscript:
    language: str
    source_type: str
    segments: tuple[TranscriptSegment, ...]

    @property
    def plain_text(self) -> str:
        return "\n".join(segment.text for segment in self.segments)
