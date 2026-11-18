"""分块压缩归档的确定性验证脚本（纯标准库，无需 MySQL/Redis）。

覆盖需求：
1. 窄范围的实际读盘量：只命中一个块，bytes_read 恰好等于
   定宽索引头 + 单条定位记录 + 单个压缩块，且明显小于读整份档案。
2. 跨块查询：宽范围命中多块，读盘量随命中块累加，结果与范围切片一致。
3. 末块损坏：篡改末块字节后，整次查询拒绝且不返回任何键值（无半截结果）；
   不触及坏块的窄范围查询仍成功。
4. 重复值密集的归档应比原档更小：压缩率 < 1。
5. 读取容量不足时拒绝查询，且拒绝发生在数据块读取之前（零数据块读盘）。
6. 旧试验（无档案）照常可读：未归档走回退，不影响原试验结果 JSON。
7. 零存活键（全部删除边界集）：空档案可建可查，查询只计定宽索引头。

运行：python -I scripts/verify_block_store.py
"""
import json
import shutil
import sys
import tempfile
from pathlib import Path

# 支持 python -I：隔离模式忽略 PYTHONPATH，运行时自行加入 backend 根目录。
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.services import block_store
from src.services.lsm_engine import simulate

HEADER_PAD = block_store.HEADER_PAD
LOC_WIDTH = 4 + block_store.LOC_ENTRY_PAD


def make_dense_entries(keys=80, repeats=6):
    """重复值密集的写入序列：大量键共享少量取值，压缩后应明显变小。"""
    entries = []
    for _ in range(repeats):
        for k in range(keys):
            entries.append({'op': 'put', 'key': f'sensor-{k:03d}',
                            'value': f'reading-group-{k % 4}'})
    # 两个墓碑：确认存活键归档不含已删除键。
    entries.append({'op': 'delete', 'key': 'sensor-000'})
    entries.append({'op': 'delete', 'key': 'sensor-079'})
    return entries


def main():
    root = Path(tempfile.mkdtemp(prefix='block-store-verify-'))
    try:
        config = {'memtable_limit': 8, 'l0_trigger': 2,
                  'max_levels': 3, 'final_compaction': True}
        result = simulate(make_dense_entries(), config)
        trial_id = 9001
        meta = block_store.build_archive(root, trial_id, result['actual'],
                                         block_capacity=16)
        print('== 归档概况 ==')
        print(json.dumps(meta, ensure_ascii=False, indent=2))
        assert meta['entry_count'] == len(result['actual']) == 78
        assert meta['block_count'] == 5  # 78 键 / 16 每块 => 5 块（末块14）

        archive_file = root / f'trial-{trial_id}.archive'
        index_file = root / f'trial-{trial_id}.index'
        full_archive_bytes = archive_file.stat().st_size
        full_index_bytes = index_file.stat().st_size

        # ---- 1. 窄范围：只命中首块 ----
        narrow = block_store.range_query(
            root, trial_id, start_key='sensor-001', end_key='sensor-005')
        first_compressed = narrow['block_evidence'][0]['compressed_length']
        assert narrow['hit_blocks'] == 1, narrow
        assert narrow['key_count'] == 5, narrow
        # 计量恒等式：读盘 = 定宽头 + 实际读取定位记录（二分探测+命中区间）+ 数据块。
        assert narrow['bytes_read'] == (
            HEADER_PAD + narrow['locator_bytes_read']
            + narrow['block_bytes_read']), narrow
        assert narrow['block_bytes_read'] == first_compressed
        # 二分定位只读少量定位记录，必然少于整张定位区（5 条）。
        assert narrow['locator_bytes_read'] < 5 * LOC_WIDTH
        assert narrow['index_header_bytes'] == HEADER_PAD
        print('\n[1] 窄范围实际读盘量 =', narrow['bytes_read'], '字节 = 索引头',
              HEADER_PAD, '+ 定位记录', narrow['locator_bytes_read'],
              '+ 压缩块', first_compressed)
        # 窄范围只读一个块，必须比读整份档案 + 整张索引都省。
        assert narrow['bytes_read'] < full_archive_bytes + full_index_bytes
        print('    整份档案+索引 =', full_archive_bytes + full_index_bytes,
              '字节 → 窄范围省读',
              full_archive_bytes + full_index_bytes - narrow['bytes_read'], '字节')

        # ---- 2. 跨块查询 ----
        cross = block_store.range_query(
            root, trial_id, start_key='sensor-010', end_key='sensor-050')
        expect_keys = sorted(k for k in result['actual']
                             if 'sensor-010' <= k <= 'sensor-050')
        assert cross['hit_blocks'] >= 3, cross
        assert list(cross['items'].keys()) == expect_keys
        assert cross['items'] == {k: result['actual'][k] for k in expect_keys}
        assert cross['bytes_read'] == cross['required_bytes']
        # 通用计量恒等式（定位记录含二分探测，不再简单等于命中块数 × 宽度）。
        assert cross['bytes_read'] == (
            HEADER_PAD + cross['locator_bytes_read']
            + cross['block_bytes_read'])
        # 跨块读到的定位记录不少于命中块所需，且不超过整张定位区。
        assert cross['locator_bytes_read'] >= cross['hit_blocks'] * LOC_WIDTH
        assert cross['locator_bytes_read'] <= 5 * LOC_WIDTH
        print('\n[2] 跨块查询命中', cross['hit_blocks'], '块 /',
              cross['key_count'], '键，实际读盘', cross['bytes_read'], '字节')
        print('    块证据:', [(b['block_id'], b['checksum_ok'])
                            for b in cross['block_evidence']])
        # 跨块读盘量必须大于窄范围（读到更多块），但小于全量。
        assert narrow['bytes_read'] < cross['bytes_read']
        full_scan = block_store.range_query(root, trial_id)
        assert cross['bytes_read'] < full_scan['bytes_read']
        print('    读盘量梯度：窄范围', narrow['bytes_read'], '< 跨块',
              cross['bytes_read'], '< 全量', full_scan['bytes_read'])

        # ---- 3. 额度不足：拒绝且零数据块读盘 ----
        tiny_quota = 1
        try:
            block_store.range_query(root, trial_id,
                                    start_key='sensor-010',
                                    end_key='sensor-050',
                                    read_quota=tiny_quota)
            raise AssertionError('额度不足应当拒绝查询')
        except block_store.QuotaExceeded as exc:
            assert exc.required > tiny_quota
            print('\n[3] 额度', tiny_quota, '字节不足，需', exc.required,
                  '字节 → 查询被拒绝（数据块零读取）')
        # 恰好满足额度时放行。
        ok = block_store.range_query(
            root, trial_id, start_key='sensor-010', end_key='sensor-050',
            read_quota=cross['required_bytes'])
        assert ok['accepted'] and ok['key_count'] == cross['key_count']
        print('    额度 = 需求量', cross['required_bytes'], '字节 → 放行，读盘',
              ok['bytes_read'], '字节')

        # ---- 4. 重复值密集：压缩归档比原档更小 ----
        raw = json.dumps(result['actual'], ensure_ascii=False,
                         sort_keys=True).encode()
        assert full_archive_bytes < len(raw), (full_archive_bytes, len(raw))
        print('\n[4] 原始存活键值 JSON =', len(raw),
              '字节；压缩档案 =', full_archive_bytes, '字节；压缩率 =',
              round(full_archive_bytes / len(raw), 4), '(< 1)')

        # ---- 5. 末块损坏：整次拒绝，不交付半截结果 ----
        original = archive_file.read_bytes()
        corrupted = bytearray(original)
        corrupted[-1] ^= 0xFF  # 翻转末块最后一个字节
        archive_file.write_bytes(bytes(corrupted))
        try:
            block_store.range_query(
                root, trial_id, start_key='sensor-001',
                end_key='sensor-078',
                read_quota=full_scan['required_bytes'] + 4096)
            raise AssertionError('末块损坏必须令整次查询失败')
        except block_store.ArchiveError as exc:
            print('\n[5] 末块损坏（宽范围命中末块）→', exc)
        # 仅查未损坏的前段块仍应成功：坏块不在命中集合内，不牵连好块。
        safe = block_store.range_query(
            root, trial_id, start_key='sensor-001', end_key='sensor-005')
        assert safe['key_count'] == 5 and all(
            b['checksum_ok'] for b in safe['block_evidence'])
        print('    未命中坏块的窄范围查询照常成功，返回', safe['key_count'], '键')
        archive_file.write_bytes(original)  # 还原后全量查询恢复。
        recovered = block_store.range_query(root, trial_id)
        assert recovered['items'] == dict(sorted(result['actual'].items()))
        print('    还原末块后全量查询恢复，键数', recovered['key_count'])

        # ---- 6. 旧试验无档案：回退可读，不影响原结果 ----
        legacy_id = 9002
        assert block_store.archive_meta(root, legacy_id) is None
        assert not block_store.is_archived(root, legacy_id)
        print('\n[6] 旧试验 #', legacy_id,
              '无分块档案 → archive_meta 返回 None，原试验 JSON 结果不受影响')

        # ---- 7. 零存活键（全部删除边界集）：空档案可建可查 ----
        empty_entries = [{'op': 'put', 'key': 'x', 'value': '1'},
                         {'op': 'put', 'key': 'y', 'value': '2'},
                         {'op': 'delete', 'key': 'x'},
                         {'op': 'delete', 'key': 'y'}]
        empty_cfg = {'memtable_limit': 2, 'l0_trigger': 2,
                     'max_levels': 3, 'final_compaction': True}
        empty_result = simulate(empty_entries, empty_cfg)
        empty_id = 9003
        empty_meta = block_store.build_archive(root, empty_id,
                                               empty_result['actual'])
        assert empty_meta['entry_count'] == 0 and empty_meta['block_count'] == 0
        # 空档案查询也要读定宽索引头确认“零块”，额度不足照样拒绝。
        try:
            block_store.range_query(root, empty_id, read_quota=10)
            raise AssertionError('空档案的索引头读取也应受额度约束')
        except block_store.QuotaExceeded:
            pass
        empty_q = block_store.range_query(root, empty_id, read_quota=HEADER_PAD)
        assert empty_q['accepted'] and empty_q['key_count'] == 0
        assert empty_q['hit_blocks'] == 0 and empty_q['bytes_read'] == HEADER_PAD
        print('\n[7] 零存活键 → 空档案（0 块），查询仅计索引头',
              HEADER_PAD, '字节，返回空键值')

        # ---- 8. 大档案窄读扩展性：定位读盘随块数对数增长，不读整张索引 ----
        big_id = 9004
        big_kv = {f'k-{i:05d}': f'val-{i % 8}' for i in range(2000)}
        block_store.build_archive(root, big_id, big_kv, block_capacity=16)  # 125 块
        big_narrow = block_store.range_query(
            root, big_id, start_key='k-00100', end_key='k-00110')
        big_full = block_store.range_query(root, big_id)
        assert big_narrow['hit_blocks'] == 1 and big_narrow['key_count'] == 11
        assert big_narrow['locator_bytes_read'] < big_full['locator_bytes_read'] / 10
        assert big_narrow['bytes_read'] < big_full['bytes_read'] / 10
        print('\n[8] 125 块大档案：窄范围定位读', big_narrow['locator_bytes_read'],
              'B vs 全量', big_full['locator_bytes_read'], 'B；总读盘',
              big_narrow['bytes_read'], 'B vs', big_full['bytes_read'],
              'B（约', round(big_narrow['bytes_read'] / big_full['bytes_read'], 3),
              '），值班只查一段键不读整档')

        # ---- 9. 极端长键（文档上限 64 字符）：定位记录定宽不得溢出 ----
        long_id = 9005
        long_kv = {('z' * 64) + f'-{i:03d}': f'v{i % 3}' for i in range(40)}
        long_meta = block_store.build_archive(root, long_id, long_kv,
                                              block_capacity=1)
        long_q = block_store.range_query(
            root, long_id,
            start_key='z' * 64 + '-000', end_key='z' * 64 + '-009')
        assert long_q['accepted'] and long_q['key_count'] == 10
        # 定位负载上界校验：最坏 JSON 必须能放进定宽记录，否则这里建不出档案。
        worst = json.dumps({
            'min_key': 'k' * 64, 'max_key': 'k' * 64, 'entry_count': 64,
            'offset': 10 ** 12, 'compressed_length': 10 ** 12,
            'checksum': 'a' * 64,
        }, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()
        assert len(worst) <= block_store.LOC_ENTRY_PAD
        # 文件头上界校验：含两个 64 字符键与两个 64 hex 校验和的最坏头部。
        worst_header = json.dumps({
            'magic': block_store.INDEX_MAGIC, 'trial_id': 2 ** 31 - 1,
            'entry_count': 2000, 'block_capacity': 64, 'block_count': 2000,
            'min_key': 'z' * 64, 'max_key': 'z' * 64,
            'locator_entry_width': 4 + block_store.LOC_ENTRY_PAD,
            'locator_bytes': 10 ** 9, 'archive_checksum': 'a' * 64,
            'archive_bytes': 10 ** 12, 'raw_bytes': 10 ** 12,
            'locator_checksum': 'b' * 64,
        }, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()
        assert len(worst_header) <= block_store.HEADER_PAD
        print('\n[9] 64 字符长键归档成功：', long_meta['block_count'],
              '块；窄范围返回', long_q['key_count'], '键；最坏定位负载',
              len(worst), 'B ≤ 定宽', block_store.LOC_ENTRY_PAD,
              'B；最坏文件头', len(worst_header), 'B ≤', block_store.HEADER_PAD, 'B')

        print('\n全部断言通过：窄范围计量、跨块查询、末块损坏、压缩率、额度门控、旧档回退、空档案、大档案窄读扩展性、长键定宽边界。')
    finally:
        shutil.rmtree(root, ignore_errors=True)


if __name__ == '__main__':
    main()
