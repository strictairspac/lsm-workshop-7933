"""分块压缩归档存储（确定性教学模型，纯标准库）。

磁盘布局（每个试验两份文件，两阶段原子落盘，同时就绪才开放查询）：

  trial-<id>.archive   数据档案：顺序拼接的 zlib 压缩块，每块带 SHA-256。
  trial-<id>.index     分块索引：定宽文件头 + 每条定宽的块定位记录。

设计要点：
- 仅归档 *已通过 Oracle 校验* 的存活键值（lsm_engine.simulate 的 actual）。
- 索引文件头为定长 JSON，记录每条定位记录的宽度；窄范围查询只读取文件头
  与“命中块”对应的定位记录，不必把整张索引读入，真实读盘量因此随范围变窄
  而下降（值班只查一段键，不读整份试验结果）。
- 每个数据块独立 zlib 压缩并带 SHA-256 校验；查询先核验全部命中块，任一坏
  块即整次拒绝，绝不交付半截结果。
- 读取容量口径 = 索引文件头 + 命中定位记录 + 命中压缩块，额度不足直接拒绝，
  且拒绝发生在任何数据块读取之前。
- 所有真实磁盘读取以字节计量，返回 bytes_read / blocks_read 证据。

这是对磁盘分块归档的教学模拟，不是 MySQL/InnoDB 内核实现。
"""
from __future__ import annotations

import hashlib
import json
import os
import struct
import zlib
from dataclasses import dataclass
from pathlib import Path

ARCHIVE_SUFFIX = '.archive'
INDEX_SUFFIX = '.index'
INDEX_MAGIC = 'LSM-BLOCK-INDEX-V2'
# 索引文件头定宽，不足补空格，便于一次定长读盘。
# 640 字节覆盖文档约束下最坏文件头：两个 64 字符键 + 两个 64 hex 校验和 +
# 各整数字段（实测最坏约 545 字节），保留余量。
HEADER_PAD = 640
DEFAULT_BLOCK_CAPACITY = 32
# 每条块定位记录定宽：4 字节定长块号 + 定宽 JSON 负载。
# 320 字节覆盖文档约束下的最坏情况：两个 64 字符键 + 64 位 SHA-256 校验和 +
# offset/length（操作上限 2000、块容量上限 64，档案体量有界），含余量。
LOC_ENTRY_PAD = 320


class ArchiveError(Exception):
    """归档损坏或不可读（坏块/截断/校验失败）。"""


class QuotaExceeded(Exception):
    """读取额度不足以覆盖定位信息与全部命中块，查询被拒绝。"""

    def __init__(self, detail: str, *, required: int, quota: int):
        super().__init__(detail)
        self.required = required
        self.quota = quota


def _canonical(value) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(',', ':')).encode('utf-8')


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _archive_paths(root, trial_id: int) -> tuple[Path, Path]:
    base = Path(root)
    return (base / f'trial-{trial_id}{ARCHIVE_SUFFIX}',
            base / f'trial-{trial_id}{INDEX_SUFFIX}')


def _chunk(items, capacity):
    return [items[i:i + capacity] for i in range(0, len(items), capacity)]


# --------------------------------------------------------------------------- 定位记录
@dataclass
class BlockMeta:
    block_id: int
    min_key: str
    max_key: str
    entry_count: int
    offset: int
    compressed_length: int
    checksum: str

    def to_locator(self) -> bytes:
        payload = _canonical({
            'min_key': self.min_key,
            'max_key': self.max_key,
            'entry_count': self.entry_count,
            'offset': self.offset,
            'compressed_length': self.compressed_length,
            'checksum': self.checksum,
        })
        if len(payload) > LOC_ENTRY_PAD:
            raise ArchiveError(
                f'块定位记录 {len(payload)} 字节超过定宽 {LOC_ENTRY_PAD}，请调大 LOC_ENTRY_PAD')
        return struct.pack('>I', self.block_id) + payload.ljust(LOC_ENTRY_PAD, b' ')

    @staticmethod
    def from_locator(raw: bytes) -> dict:
        block_id = struct.unpack('>I', raw[:4])[0]
        payload = json.loads(raw[4:].rstrip(b' '))
        return {'block_id': block_id, **payload}


# --------------------------------------------------------------------------- 建索引/档案
def build_archive(root, trial_id: int, live_kv: dict,
                  block_capacity: int = DEFAULT_BLOCK_CAPACITY) -> dict:
    """把通过校验的存活键值压缩归档，索引与档案两阶段原子落盘。"""
    if block_capacity < 1:
        raise ValueError('块容量必须大于等于1')
    archive_path, index_path = _archive_paths(root, trial_id)
    archive_tmp = archive_path.with_suffix(ARCHIVE_SUFFIX + '.tmp')
    index_tmp = index_path.with_suffix(INDEX_SUFFIX + '.tmp')
    archive_path.parent.mkdir(parents=True, exist_ok=True)

    items = sorted(live_kv.items())
    blocks: list[BlockMeta] = []
    payload = bytearray()
    raw_total = 0
    for block_id, chunk in enumerate(_chunk(items, block_capacity)):
        raw = _canonical(chunk)
        raw_total += len(raw)
        packed = zlib.compress(raw, level=9)
        blocks.append(BlockMeta(
            block_id=block_id,
            min_key=chunk[0][0],
            max_key=chunk[-1][0],
            entry_count=len(chunk),
            offset=len(payload),
            compressed_length=len(packed),
            checksum=_sha256(packed),
        ))
        payload.extend(packed)
    payload_bytes = bytes(payload)

    locator_bytes = b''.join(b.to_locator() for b in blocks)
    header = {
        'magic': INDEX_MAGIC,
        'trial_id': trial_id,
        'entry_count': len(items),
        'block_capacity': block_capacity,
        'block_count': len(blocks),
        'min_key': items[0][0] if items else None,
        'max_key': items[-1][0] if items else None,
        'locator_entry_width': 4 + LOC_ENTRY_PAD,
        'locator_bytes': len(locator_bytes),
        'archive_checksum': _sha256(payload_bytes),
        'archive_bytes': len(payload_bytes),
        'raw_bytes': raw_total,
        # 头部不随块数增长，窄范围只需读这一个定宽头即可知道定位记录布局。
        'locator_checksum': _sha256(locator_bytes),
    }
    header_bytes = _canonical(header).ljust(HEADER_PAD, b' ')
    if len(header_bytes) != HEADER_PAD:
        raise ArchiveError(f'索引文件头超过定宽 {HEADER_PAD}')
    index_bytes = header_bytes + locator_bytes

    # 阶段一：临时文件落盘并 fsync。
    archive_tmp.write_bytes(payload_bytes)
    index_tmp.write_bytes(index_bytes)
    for tmp in (archive_tmp, index_tmp):
        with open(tmp, 'rb') as fh:
            os.fsync(fh.fileno())
    # 阶段二：原子替换，两份同时生效；崩溃只会留下 .tmp，查询看不到半成品。
    os.replace(archive_tmp, archive_path)
    os.replace(index_tmp, index_path)
    return {
        'archived': True,
        'root': str(Path(root)),
        'entry_count': len(items),
        'block_count': len(blocks),
        'block_capacity': block_capacity,
        'min_key': header['min_key'],
        'max_key': header['max_key'],
        'archive_bytes': len(payload_bytes),
        'raw_bytes': raw_total,
        'index_bytes': len(index_bytes),
        'index_header_bytes': HEADER_PAD,
        'compression_ratio': round(len(payload_bytes) / raw_total, 4) if raw_total else 0,
        'archive_checksum': header['archive_checksum'],
        'locator_checksum': header['locator_checksum'],
        'archive_file': archive_path.name,
        'index_file': index_path.name,
    }


# --------------------------------------------------------------------------- 读索引
def is_archived(root, trial_id: int) -> bool:
    archive_path, index_path = _archive_paths(root, trial_id)
    return archive_path.is_file() and index_path.is_file()


def _read_header(index_path: Path) -> dict:
    with open(index_path, 'rb') as fh:
        raw = fh.read(HEADER_PAD)
    if len(raw) != HEADER_PAD:
        raise ArchiveError('索引文件头截断，定位信息不可读')
    try:
        header = json.loads(raw.rstrip(b' '))
    except json.JSONDecodeError as exc:
        raise ArchiveError(f'索引文件头无法解析：{exc}') from exc
    if header.get('magic') != INDEX_MAGIC:
        raise ArchiveError('索引格式无法识别')
    return header


def _read_all_locators(index_path: Path, header: dict) -> list[dict]:
    """读取整张定位区并校验，用于建档自检与全量完整性核验。"""
    width = header['locator_entry_width']
    with open(index_path, 'rb') as fh:
        fh.seek(HEADER_PAD)
        raw = fh.read(header['block_count'] * width)
    if len(raw) != header['block_count'] * width:
        raise ArchiveError('索引定位区截断')
    if _sha256(raw) != header['locator_checksum']:
        raise ArchiveError('索引定位区校验失败，块定位信息不可信')
    return [BlockMeta.from_locator(raw[i:i + width])
            for i in range(0, len(raw), width)]


def verify_integrity(root, trial_id: int) -> dict:
    """全量自检：校验索引定位区与每个压缩块的 SHA-256，供建档后确认与排障。"""
    archive_path, index_path = _archive_paths(root, trial_id)
    if not (archive_path.is_file() and index_path.is_file()):
        raise ArchiveError('索引与档案未同时就绪')
    header = _read_header(index_path)
    metas = _read_all_locators(index_path, header)
    with open(archive_path, 'rb') as fh:
        payload = fh.read()
    if _sha256(payload) != header['archive_checksum']:
        raise ArchiveError('档案整体校验失败')
    bad = []
    for block in metas:
        packed = payload[block['offset']:block['offset'] + block['compressed_length']]
        if len(packed) != block['compressed_length'] or _sha256(packed) != block['checksum']:
            bad.append(block['block_id'])
    if bad:
        raise ArchiveError(f"坏块：{bad}")
    return {'trial_id': trial_id, 'blocks_verified': len(metas), 'intact': True}


def archive_meta(root, trial_id: int) -> dict | None:
    """供页面/接口展示归档概况；未归档返回 None（旧试验照常可读，不强制归档）。"""
    if not is_archived(root, trial_id):
        return None
    _, index_path = _archive_paths(root, trial_id)
    header = _read_header(index_path)
    return {
        'archived': True,
        'entry_count': header['entry_count'],
        'block_count': header['block_count'],
        'block_capacity': header['block_capacity'],
        'min_key': header['min_key'],
        'max_key': header['max_key'],
        'archive_bytes': header['archive_bytes'],
        'index_bytes': HEADER_PAD + header['locator_bytes'],
        'index_header_bytes': HEADER_PAD,
        'raw_bytes': header['raw_bytes'],
        'compression_ratio': round(header['archive_bytes'] / header['raw_bytes'], 4)
        if header['raw_bytes'] else 0,
        'archive_checksum': header['archive_checksum'],
        'locator_checksum': header['locator_checksum'],
    }


def _read_one_locator(index_path: Path, header: dict, block_id: int) -> dict:
    """seek 读取单条定宽定位记录（窄范围二分定位用，不触碰其它块记录）。"""
    width = header['locator_entry_width']
    if not 0 <= block_id < header['block_count']:
        raise ArchiveError(f'定位记录块号 {block_id} 越界')
    with open(index_path, 'rb') as fh:
        fh.seek(HEADER_PAD + block_id * width)
        raw = fh.read(width)
    if len(raw) != width:
        raise ArchiveError(f'块 {block_id} 定位记录截断')
    return BlockMeta.from_locator(raw)


def _find_hit_span(index_path: Path, header: dict, lo: str, hi: str):
    """块按全局键序等容切分，min/max 单调，二分求与 [lo,hi] 相交的连续块区间。

    只 seek 读取 O(log B) 条定位记录做边界探测；返回 (first, last, 探测块号集合)，
    无相交时 first > last。
    """
    total = header['block_count']
    probes: set[int] = set()

    def max_of(b: int) -> str:
        probes.add(b)
        return _read_one_locator(index_path, header, b)['max_key']

    def min_of(b: int) -> str:
        probes.add(b)
        return _read_one_locator(index_path, header, b)['min_key']

    # 首个 max_key >= lo 的块（max 随块号严格递增，谓词单调）。
    left, right = 0, total
    while left < right:
        mid = (left + right) // 2
        if max_of(mid) >= lo:
            right = mid
        else:
            left = mid + 1
    first = left
    # 最后一个 min_key <= hi 的块（min 随块号递增，谓词先真后假）。
    left, right = 0, total
    while left < right:
        mid = (left + right) // 2
        if min_of(mid) <= hi:
            left = mid + 1
        else:
            right = mid
    last = left - 1
    return first, last, probes


def _read_locator_span(index_path: Path, header: dict,
                       first: int, last: int) -> list[dict]:
    """连续读取 [first,last] 块的定位记录（一次顺序读）。"""
    width = header['locator_entry_width']
    out = []
    with open(index_path, 'rb') as fh:
        fh.seek(HEADER_PAD + first * width)
        raw = fh.read((last - first + 1) * width)
    if len(raw) != (last - first + 1) * width:
        raise ArchiveError('命中块定位区间截断')
    for i in range(last - first + 1):
        out.append(BlockMeta.from_locator(raw[i * width:(i + 1) * width]))
    return out


# --------------------------------------------------------------------------- 范围查询
def range_query(root, trial_id: int, *, start_key=None, end_key=None,
                read_quota=None) -> dict:
    """按键范围读取归档值，全程计量真实读盘量，任何一步失败都不返回键值。

    块按全局键序等容切分、min/max 单调，因此先凭定宽文件头（一次读盘）做二分，
    只 seek 探测 O(log B) 条定位记录即可确定相交的连续块区间 [first,last]，
    窄范围不必读整张定位区：

    1. 读定宽索引头（一次读盘）；范围在全档键界之外直接判定零命中；
    2. 二分定位命中块区间（仅读边界探测所需的定位记录）；
    3. 用“索引头 + 实际会读的定位记录 + 命中压缩块”预估容量，额度不足直接拒绝
       （拒绝发生在数据块读取之前）；
    4. 顺序读取命中块定位区间与全部命中数据块，逐块 SHA-256 校验、解压、核条目；
    5. 全部命中块核验通过后才按键范围过滤返回键值（坏块不交付半截结果）。
    """
    archive_path, index_path = _archive_paths(root, trial_id)
    if not (archive_path.is_file() and index_path.is_file()):
        raise ArchiveError('索引与档案未同时就绪，查询未开放')
    header = _read_header(index_path)
    lo = start_key if start_key is not None else ''
    hi = end_key if end_key is not None else '￿'
    header_bytes = HEADER_PAD
    locator_width = header['locator_entry_width']

    # 零块档案或范围完全落在全档键界之外：只读文件头即可判定。
    g_min, g_max = header['min_key'], header['max_key']
    if not header['block_count'] or g_min is None or hi < g_min or lo > g_max:
        required_bytes = header_bytes
        if read_quota is not None and read_quota < required_bytes:
            raise QuotaExceeded(
                f'读取容量不足：判定零命中仍需读索引头 {required_bytes} 字节，'
                f'额度仅 {read_quota} 字节，查询已拒绝',
                required=required_bytes, quota=read_quota,
            )
        return {
            'trial_id': trial_id, 'start_key': start_key, 'end_key': end_key,
            'read_quota': read_quota, 'accepted': True, 'hit_blocks': 0,
            'blocks_read': 0, 'bytes_read': header_bytes,
            'required_bytes': required_bytes, 'index_header_bytes': header_bytes,
            'locator_bytes_read': 0, 'block_bytes_read': 0,
            'block_evidence': [], 'key_count': 0, 'items': {},
            'metering': '范围在全档键界之外或空档案，仅读定宽索引头即判定零命中',
        }

    first, last, probes = _find_hit_span(index_path, header, lo, hi)
    if first > last:
        required_bytes = header_bytes
        if read_quota is not None and read_quota < required_bytes:
            raise QuotaExceeded(
                f'读取容量不足：判定零命中仍需读索引头 {required_bytes} 字节，'
                f'额度仅 {read_quota} 字节，查询已拒绝',
                required=required_bytes, quota=read_quota,
            )
        return {
            'trial_id': trial_id, 'start_key': start_key, 'end_key': end_key,
            'read_quota': read_quota, 'accepted': True, 'hit_blocks': 0,
            'blocks_read': 0, 'bytes_read': header_bytes,
            'required_bytes': required_bytes, 'index_header_bytes': header_bytes,
            'locator_bytes_read': 0, 'block_bytes_read': 0,
            'block_evidence': [], 'key_count': 0, 'items': {},
            'metering': '二分定位后无相交块，仅读定宽索引头即判定零命中',
        }

    span_ids = set(range(first, last + 1))
    # 实际读取的定位记录：二分探测块与命中区间去重后计数，保证计量等于真实读盘。
    locator_ids_read = probes | span_ids
    targeted = _read_locator_span(index_path, header, first, last)
    # 用真实块键界复核相交性（二分已保证，这里防御性确认）。
    targeted = [b for b in targeted if b['max_key'] >= lo and b['min_key'] <= hi]

    locator_bytes = len(locator_ids_read) * locator_width
    block_bytes = sum(b['compressed_length'] for b in targeted)
    required_bytes = header_bytes + locator_bytes + block_bytes

    if read_quota is not None and read_quota < required_bytes:
        raise QuotaExceeded(
            f'读取容量不足：本次需读 {required_bytes} 字节（索引头 {header_bytes}'
            f' + 定位记录 {locator_bytes} + 命中块 {block_bytes}），'
            f'额度仅 {read_quota} 字节，查询已拒绝',
            required=required_bytes, quota=read_quota,
        )

    verified: dict[str, str] = {}
    bytes_read = header_bytes + locator_bytes
    block_evidence = []
    with open(archive_path, 'rb') as fh:
        for block in targeted:
            fh.seek(block['offset'])
            packed = fh.read(block['compressed_length'])
            bytes_read += len(packed)
            if len(packed) != block['compressed_length']:
                raise ArchiveError(f"块 {block['block_id']} 截断：档案疑似损坏")
            if _sha256(packed) != block['checksum']:
                raise ArchiveError(
                    f"块 {block['block_id']} 校验失败：档案损坏，拒绝交付半截结果")
            try:
                raw = zlib.decompress(packed)
                entries = json.loads(raw)
            except (zlib.error, json.JSONDecodeError) as exc:
                raise ArchiveError(f"块 {block['block_id']} 解压失败：{exc}") from exc
            if not isinstance(entries, list) or len(entries) != block['entry_count']:
                raise ArchiveError(f"块 {block['block_id']} 条目数异常，档案不可信")
            for key, value in entries:
                if lo <= key <= hi:
                    verified[key] = value
            block_evidence.append({
                'block_id': block['block_id'],
                'min_key': block['min_key'],
                'max_key': block['max_key'],
                'entry_count': block['entry_count'],
                'compressed_length': block['compressed_length'],
                'checksum_ok': True,
            })

    return {
        'trial_id': trial_id,
        'start_key': start_key,
        'end_key': end_key,
        'read_quota': read_quota,
        'accepted': True,
        'hit_blocks': len(targeted),
        'blocks_read': len(targeted),
        'bytes_read': bytes_read,
        'required_bytes': required_bytes,
        'index_header_bytes': header_bytes,
        'locator_bytes_read': locator_bytes,
        'block_bytes_read': block_bytes,
        'block_evidence': block_evidence,
        'key_count': len(verified),
        'items': dict(sorted(verified.items())),
        'metering': 'bytes_read = 定宽索引头 + 二分探测与命中区间的定位记录 + 实际读取的命中压缩块；未命中块不读盘',
    }
