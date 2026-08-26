"""Cắt & ghép nhiều đoạn từ nhiều video nguồn thành một video liền mạch.

Đầu vào là "số hiệu video + đoạn cần lấy", thứ tự liệt kê chính là thứ tự
ghép. Ghép xong ra một file duy nhất, sau đó pipeline render bình thường
(phụ đề, text bìa, nhạc nền) chạy tiếp trên file đó — hoặc tải luôn nếu không
khai thêm gì.

Cách ghép: một lệnh ffmpeg duy nhất. Không dùng concat demuxer vì nó đòi mọi
đoạn phải cùng codec/độ phân giải/fps — trong khi mục đích ở đây chính là ghép
các video quay bằng máy khác nhau. Chuẩn hoá từng đoạn (scale/pad/fps) ngay
trong cùng một lệnh filter_complex, đỡ được một vòng ghi file trung gian
(``/tmp`` trên Cloud Run là RAM).

Nối các đoạn bằng ``xfade``/``acrossfade`` (crossfade) khi ``transition.enabled``
— mặc định bật, vì cắt cứng giữa hai cảnh là đúng thứ gây cảm giác giật. Chỉ
lùi về filter ``concat`` (cắt cứng, không tốn thời gian chồng hình) khi tắt
hiệu ứng hoặc khi chỉ có một đoạn (không có gì để chuyển cảnh).

Chuẩn hoá khung hình bằng ``scale=...:force_original_aspect_ratio=decrease``
kèm ``pad``: video khác tỉ lệ được thu vừa khung rồi chèn viền, KHÔNG bao giờ
bị kéo giãn cho vừa. ``setsar=1`` chốt lại pixel vuông.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from app.clip_syntax import parse_clip_lines
from app.clip_transitions import audio_xfade_chain, total_overlap_seconds, video_xfade_chain
from app.models import ClipSpec, RenderOptions
from app.probe_data import ProbeResult
from app.utils import InvalidOptions

# parse_clip_lines chuyển sang app/clip_syntax.py để giữ file này dưới 400
# dòng; re-export ở đây cho các chỗ đã quen ``from app.clips import
# parse_clip_lines`` (xem __all__ bên dưới).

__all__ = [
    "SourceVideo",
    "ResolvedClip",
    "MERGED_NAME",
    "parse_clip_lines",
    "resolve_clips",
    "resolve_source_index",
    "source_labels",
    "select_sources",
    "resolve_concat_canvas",
    "build_concat_command",
    "total_duration",
    "merged_duration",
]

MERGED_NAME: Final[str] = "merged.mp4"
_ANULLSRC: Final[str] = "anullsrc=channel_layout=stereo:sample_rate=48000"
_AFORMAT: Final[str] = "aformat=sample_fmts=fltp:sample_rates=48000:channel_layouts=stereo"
_FALLBACK_FPS: Final[float] = 30.0
# Trên 60fps thì file phồng to mà mắt gần như không phân biệt được, và /tmp là RAM.
_MAX_FPS: Final[float] = 60.0
# Ghép là vòng encode ĐẦU; nếu sau đó còn burn phụ đề thì video bị encode lần
# hai. Hạ crf vài bậc ở vòng đầu để lần hai không ăn vào chất lượng thấy được.
_INTERMEDIATE_CRF_BONUS: Final[int] = 3


@dataclass(frozen=True, slots=True)
class SourceVideo:
    """Một video nguồn đã nằm trong workspace và đã probe xong."""

    name: str  # tên file tương đối trong workspace (src1.mp4, input.mp4...)
    probe: ProbeResult
    # Tên file GỐC người dùng gửi lên (tên file upload, hoặc tên trên Drive) —
    # dùng để khớp ``clips[].source`` khai bằng tên thay vì số hiệu.
    label: str = ""


@dataclass(frozen=True, slots=True)
class ResolvedClip:
    """Đoạn đã chốt: file nào, từ giây nào, dài bao lâu."""

    name: str
    start: float
    duration: float
    has_audio: bool


def source_labels(sources: list[SourceVideo]) -> list[str]:
    """Tên dùng để khớp cho từng nguồn: tên file gốc, thiếu thì tên trong workspace."""
    return [item.label or item.name for item in sources]


def resolve_source_index(source: int | str, labels: list[str], position: int) -> int:
    """Chỉ số 0-based của video nguồn mà một đoạn trỏ tới.

    ``labels`` là danh sách TÊN FILE GỐC theo đúng thứ tự nguồn — nhận list tên
    chứ không nhận list :class:`SourceVideo` để còn khớp được TRƯỚC khi tải
    file về (lúc đó mới chỉ có metadata của Drive).

    ``source`` là số thì hiểu là số hiệu (đánh từ 1). Là chuỗi thì khớp theo
    tên, dò dần từ chặt tới lỏng: khớp đúng -> không phân biệt hoa thường ->
    bỏ phần thư mục -> chứa trong tên. Nới dần như vậy vì kịch bản dựng do
    pipeline khác sinh ra hay kèm đường dẫn hoặc lệch hoa thường, mà vẫn phải
    báo lỗi khi tên khớp vào nhiều video cùng lúc.
    """
    if isinstance(source, int):
        if not 1 <= source <= len(labels):
            raise InvalidOptions(
                f"Đoạn thứ {position} trỏ tới video số {source} "
                f"nhưng chỉ có {len(labels)} video được gửi lên"
            )
        return source - 1

    wanted = source.strip()
    tail = wanted.replace("\\", "/").rsplit("/", 1)[-1].lower()
    for candidates in (
        [i for i, name in enumerate(labels) if name == wanted],
        [i for i, name in enumerate(labels) if name.lower() == wanted.lower()],
        [i for i, name in enumerate(labels) if name.rsplit("/", 1)[-1].lower() == tail],
        [i for i, name in enumerate(labels) if tail and tail in name.lower()],
    ):
        if len(candidates) == 1:
            return candidates[0]
        if len(candidates) > 1:
            matched = ", ".join(labels[i] for i in candidates)
            raise InvalidOptions(
                f"Đoạn thứ {position}: tên {source!r} khớp với nhiều video ({matched}), "
                "dùng số hiệu video cho rõ ràng"
            )
    raise InvalidOptions(
        f"Đoạn thứ {position} trỏ tới video {source!r} nhưng không có video nào tên vậy",
        detail="Các video hiện có: " + ", ".join(labels),
    )


def select_sources(
    specs: list[ClipSpec], labels: list[str]
) -> tuple[list[int], list[ClipSpec]]:
    """Những nguồn THẬT SỰ được dùng, và clips đã quy hết về tên file.

    Dùng trước khi tải: quét thư mục ra danh sách tên, đối chiếu với kịch bản
    rồi chỉ tải đúng những video có mặt trong kịch bản. Thư mục 20 video mà
    kịch bản dùng 4 thì tải cả 20 là phí — ``/tmp`` trên Cloud Run là RAM.

    Quy ``source`` về TÊN FILE luôn, vì sau khi bỏ bớt nguồn không dùng thì số
    hiệu cũ không còn trỏ đúng chỗ nữa.

    ``specs`` rỗng -> dùng tất cả, và không có gì để quy đổi.
    """
    if not specs:
        return list(range(len(labels))), []

    used: list[int] = []
    rewritten: list[ClipSpec] = []
    for position, spec in enumerate(specs, start=1):
        index = resolve_source_index(spec.source, labels, position)
        if index not in used:
            used.append(index)
        rewritten.append(spec.model_copy(update={"source": labels[index]}))
    return sorted(used), rewritten


def resolve_clips(specs: list[ClipSpec], sources: list[SourceVideo]) -> list[ResolvedClip]:
    """Đối chiếu từng đoạn với video nguồn thật, chốt lại mốc và độ dài.

    ``specs`` rỗng nghĩa là "ghép trọn vẹn từng video theo thứ tự đã nhận".
    """
    if not sources:
        raise InvalidOptions("Không có video nguồn nào để ghép")
    wanted = specs or [ClipSpec(source=index) for index in range(1, len(sources) + 1)]
    labels = source_labels(sources)

    resolved: list[ResolvedClip] = []
    for position, spec in enumerate(wanted, start=1):
        source = sources[resolve_source_index(spec.source, labels, position)]
        total = source.probe.duration
        stop = spec.stop_at()
        if stop is None:
            if total <= 0:
                raise InvalidOptions(
                    f"Đoạn thứ {position}: không đọc được độ dài của video "
                    f"{source.label or source.name}, phải khai rõ end hoặc duration"
                )
            stop = total
        elif total > 0:
            stop = min(stop, total)

        duration = stop - spec.start
        if duration <= 0:
            raise InvalidOptions(
                f"Đoạn thứ {position} rỗng: video {source.label or source.name} dài "
                f"{total:.2f}s mà đoạn yêu cầu là {spec.start:.2f}s -> {stop:.2f}s"
            )
        resolved.append(
            ResolvedClip(
                name=source.name,
                start=spec.start,
                duration=duration,
                has_audio=source.probe.has_audio,
            )
        )
    return resolved


def total_duration(clips: list[ResolvedClip]) -> float:
    """Tổng độ dài các đoạn TRƯỚC khi ghép (chưa trừ phần chồng do crossfade).

    Dùng để validate/hiển thị độ dài "danh nghĩa". Muốn độ dài THẬT của file
    ghép xong (để theo dõi tiến độ ffmpeg cho chính xác) thì dùng
    :func:`merged_duration`.
    """
    return sum(clip.duration for clip in clips)


def merged_duration(clips: list[ResolvedClip], opts: RenderOptions) -> float:
    """Độ dài THẬT của file sau khi ghép — dùng để theo dõi tiến độ ffmpeg.

    Mỗi lần crossfade làm hai đoạn chồng lên nhau ``duration`` giây, tức tổng
    thời lượng ngắn đi bấy nhiêu so với cộng dồn đơn thuần (xem
    app/clip_transitions.py).
    """
    total = total_duration(clips)
    if len(clips) <= 1 or not opts.transition.enabled:
        return total
    overlap = total_overlap_seconds(clips, opts.transition.duration)
    return max(0.0, total - overlap)


def resolve_concat_canvas(
    clips: list[ResolvedClip], sources: list[SourceVideo], opts: RenderOptions
) -> tuple[int, int, float]:
    """Khung hình và fps của video ghép: ``(width, height, fps)``.

    Mặc định lấy đúng khung của video chứa ĐOẠN ĐẦU TIÊN — đoán mò kiểu khác
    (lấy khung lớn nhất, lấy tỉ lệ phổ biến nhất) chỉ làm kết quả khó đoán.
    Muốn khác thì khai thẳng ``output.resolution``.

    fps lấy cao nhất trong các nguồn được dùng để đoạn quay mượt không bị ép
    xuống theo đoạn quay giật.
    """
    by_name = {source.name: source.probe for source in sources}
    used = [by_name[clip.name] for clip in clips if clip.name in by_name]

    if opts.output.resolution is not None:
        raw_width, raw_height = opts.output.resolution.split("x", 1)
        width, height = int(raw_width), int(raw_height)
    else:
        first = used[0] if used else None
        width = first.width if first and first.width > 0 else 0
        height = first.height if first and first.height > 0 else 0
        if width <= 0 or height <= 0:
            raise InvalidOptions("Không đọc được kích thước của video nguồn đầu tiên")
        # yuv420p/H.264 từ chối cạnh lẻ.
        width -= width % 2
        height -= height % 2

    if opts.output.fps is not None:
        fps = float(opts.output.fps)
    else:
        candidates = [probe.fps for probe in used if probe.fps > 0]
        fps = min(max(candidates), _MAX_FPS) if candidates else _FALLBACK_FPS
    return width, height, fps


def build_concat_command(
    clips: list[ResolvedClip],
    width: int,
    height: int,
    fps: float,
    opts: RenderOptions,
    *,
    output_name: str = MERGED_NAME,
    threads: int = 0,
) -> list[str]:
    """argv ffmpeg ghép các đoạn lại. Hàm thuần — không chạy gì, không I/O.

    ffmpeg sẽ chạy với ``cwd`` = workspace nên mọi tên file là tên tương đối.
    """
    if not clips:
        raise InvalidOptions("Không có đoạn nào để ghép")

    out = opts.output
    cmd: list[str] = [
        "ffmpeg",
        "-y",
        "-hide_banner",
        "-nostdin",
        "-loglevel",
        "error",
        "-progress",
        "pipe:1",
        "-nostats",
    ]
    if threads > 0:
        cmd += ["-threads", str(threads)]

    # -ss ĐỨNG TRƯỚC -i: ffmpeg seek tới keyframe gần nhất rồi giải mã tiếp cho
    # đúng mốc, nhanh hơn hẳn -ss sau -i (đọc tuần tự từ đầu file) mà vẫn chính
    # xác. -t giới hạn lượng dữ liệu đọc vào, tính từ mốc -ss.
    for clip in clips:
        cmd += ["-ss", _fmt(clip.start), "-t", _fmt(clip.duration), "-i", clip.name]

    silence_input: dict[int, int] = {}
    next_index = len(clips)
    for position, clip in enumerate(clips):
        if clip.has_audio:
            continue
        # Đoạn không có tiếng: phải cấp một luồng im lặng đúng độ dài, nếu
        # không filter concat sẽ lệch số luồng giữa các đoạn và báo lỗi.
        cmd += ["-f", "lavfi", "-t", _fmt(clip.duration), "-i", _ANULLSRC]
        silence_input[position] = next_index
        next_index += 1

    chains: list[str] = []
    labels: list[str] = []
    for position, clip in enumerate(clips):
        # fps ĐỨNG SAU CÙNG (sau setpts): đã đo bằng ffmpeg thật trên container
        # (7.1.5) là setpts không truyền tiếp metadata frame_rate của link cho
        # filter kế tiếp. Ghép các nguồn có fps gốc KHÁC NHAU (rất hay gặp —
        # video tải từ nhiều nền tảng/máy khác nhau) thì filter xfade phía sau
        # nhận link "current rate of 1/0" và từ chối toàn bộ (lỗi 234), dù mỗi
        # đoạn RIÊNG LẺ hay ghép bằng concat vẫn ra file bình thường. Đặt fps
        # sau setpts thì chính filter fps là nơi VIẾT LẠI frame_rate của link,
        # nên giá trị luôn đúng bất kể thứ tự các filter đứng trước nó.
        chains.append(
            f"[{position}:v]scale={width}:{height}:force_original_aspect_ratio=decrease,"
            f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:color=black,setsar=1,"
            f"format=yuv420p,setpts=PTS-STARTPTS,fps={_fmt(fps)}[v{position}]"
        )
        audio_source = silence_input.get(position, position)
        # apad + atrim: chốt đúng độ dài tiếng bằng độ dài hình. Có file mà
        # track tiếng ngắn hơn track hình vài chục ms, để nguyên thì concat
        # đẩy lệch tiếng của mọi đoạn phía sau.
        chains.append(
            f"[{audio_source}:a]{_AFORMAT},apad,atrim=duration={_fmt(clip.duration)},"
            f"asetpts=PTS-STARTPTS[a{position}]"
        )
        labels.append(f"[v{position}][a{position}]")

    use_transition = len(clips) > 1 and opts.transition.enabled
    if use_transition:
        chains.append(video_xfade_chain(clips, opts.transition.style, opts.transition.duration))
        chains.append(audio_xfade_chain(clips, opts.transition.duration))
    else:
        chains.append(f"{''.join(labels)}concat=n={len(clips)}:v=1:a=1[v][a]")

    cmd += ["-filter_complex", ";".join(chains), "-map", "[v]", "-map", "[a]"]
    cmd += [
        "-c:v",
        out.video_codec,
        "-preset",
        out.preset,
        "-crf",
        str(max(0, out.crf - _INTERMEDIATE_CRF_BONUS)),
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        out.audio_codec,
        "-b:a",
        out.audio_bitrate,
        "-ar",
        "48000",
        "-movflags",
        "+faststart",
        output_name,
    ]
    return cmd


def _fmt(value: float) -> str:
    """Số cho argv: bỏ '.0' cho gọn và khớp snapshot test."""
    if float(value).is_integer():
        return str(int(value))
    text = f"{value:.6f}".rstrip("0").rstrip(".")
    return text or "0"
