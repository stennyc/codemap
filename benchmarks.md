# code_map benchmarks

Owner-gated performance gates + campaign data. Gate numbers are hard:

| gate | budget | probe |
|---|---|---|
| G1 query | QUERY_GATE_MS=50 | `code_map_query` round-trip wall |
| G2 walk | WALK_GATE_S=2.0 | whole-target BuildScanner walk |
| G3 read-side sync rebuild | READ_HEAL_MAX_FILES=2000 / MAX_ATTEMPTS=3 | per-root guard in hook.py |
| G4 query (30k-scale) | 50 ms @ 30,000 files | synthetic target |

## Gates provenance

- G4 gate first passed 2026-11-13 with a synthetic 30,000-file target
  (2,606,990 rows): 0.018 ms median / 0.024 p95 under the 50 ms gate.
- The "5s rebuild storm" incident (2026-11-06, 30 s interval,
  2,700 files) was the G2/WALK_GATE_S origin; see
  `docs/owner_rulings/2026-11-05-walk-gate.md`.

## L2.4 gate + live probe

- `QUERY_FUZZY_GATE_MS=150` (owner-gated, looser than G1: the fuzzy path
  does a ranked LIKE-prefix sweep + rarity sort, not a point lookup).
- Live probe 2026-11-17 on filemem target: fuzzy="dirty" → 1 candidate
  `test_dirty_shapes_degrade_to_default_pass` (tier-3 substring, rarity
  ranked first among the few), 98 ms.
- Honest: the filemem tree has few `dirty`-bearing names; the sweep is
  cheap, not the answer being deep.

## Campaign L2.6 (2026-11-17): "加速了解代码" 五臂对照

**任务设计（GT 由 owner 在臂起跑前独立算好，臂答案只与 GT 对分）**
- T-A 继承面枚举「filemem 树所有 class X(Base) 行」——TA-INDEX（kind=extends 全量+翻页纪律）vs TA-GREP（词面）；GT 3 行（FileMemProvider(MemoryProvider) __init__.py:2696 / _TokenRejected(Exception) shared_memory.py:286 / _BrokenTokenize(Exception) test_p1_2_topic_guard.py:201，±源树漂移——当日 llm_caller.py 增删、db 31317→35000 行）。
- T-B 概念→名（桥首报）：5 概念词（min score / dirty / supersede / bringback / stale），TB-BRIDGE 只许 fuzzy=true limit=3 + 每词至多 1 次拆词补查；GT 目标名集合 = min_score / dirty_injection_sweep 族 / filemem_supersede+confirm_supersede+_tool_supersede 族 / _bringback_ 族 / _mark_stale_observations+_graph_pull_if_stale+_force_stale；指标 = 每词 top-3 命中正确名数（0..1 ×5 分制），核验必须靠 sig/定向 read_file。
- T-C 修改影响面「min_score 0.3→0.5 波及哪些入口级函数/类」——TC-INDEX（parameter→kwarg_usage 翻页→depth=2 hop-2 def 侧）vs TC-GREP（词面+精读）；GT b 段 = 84 行/19 文件（逐文件分布已算，__init__.py 6 行 + 18 测试文件）；c 段 = 各臂「入口候选」对 hop-2 def 侧锚集报召回率。
**口径**：源树在动，GT 以起跑时刻为准、漂移如实标注；宿主钩子全局 attach 使 grep 臂背着 3.5KB policy + 符号 attach 块（历史口径不变）；n=1/臂、单目标（128 文件/35k 行）；grep 臂禁 code_map 工具（transcript 记录违规，计口径备注）。
**判分**：准确性（逐行对 GT；索引行⊄正则行的差集 = 注释/docstring/位置传参，逐条归因，方向与 L2.2 一致：索引比词面精确）+ 效率（有效调用/总调用，误调用单列）+ T-B 首报命中率 + T-C c 段召回率。
**臂结果（deleg_8ae4c877，2026-11-17 01:11 起跑，filemem 35000 行库，宿主侧 db 核验过）**

| 臂 | 有效/总调用 | 墙钟 | 准确性（vs 库/正则核验） |
|---|---|---|---|
| TA-INDEX | 24/27（2 次误调 + 3 次边界踩坑） | 309 s | 4/4 行全对（含嵌套类 BoundStore，我的 `^class` 正则 GT 漏的那行它没漏） |
| TA-GREP | 4（8 子步） | 105 s | 4/4 行全对 + 3 个 .bak 噪声行被正确标出 |
| TB-BRIDGE | 5 主查 + 2 补查 + 2 核验读 | 117 s | 1/5 干净命中（stale→is_stale）；min score 臂自判 `score` 命中（宽松），严格 GT 计 0；dirty/supersede/bringback 三族 GT 漂移（源码当日重构，库内 0 行） |
| TC-INDEX | 26/28（2 次 root 写错被拒） | 281 s | a 段 4 个活声明点与 grep 臂逐行一致；b 段 total=84 = 正则 GT 84 ✓（文件分布 ±源动） |
| TC-GREP | 7 api（~30 子步） | 353 s | b 段 101 行/24 文件（= 84 语法节点 + 17 文本提及：.bak×15/文档×2）；c 段语义最富（host hook/doc-sync/.bak 审计） |

**三项新发现的缺口（按性价比）**
1. **L2.4b 候选层三缺陷**（TB 3/5 miss 的根因，全部 db 核验）：
   - 候选列表按**行**不按**名**——`score` 6 行（多为 call_site）挤占 limit=3，`min_score`（含 min+score 双词根）被挤出：应按名去重（一行一名 + total 计数 + 多词根 boost）；
   - 前缀 tier 不剥前导下划线——`_bringback_*`/`_supersede_*` 实现符号系统性掉出 tier-2，测试 kwarg/函数名顶上来；
   - 概念词做后缀时（confirm_**supersede**）掉 tier-3，输给短 kwarg 前缀名。
2. **L2.3b 根级 kind 聚合缺失**（TA 索引臂输掉效率的直接原因）：`kind=extends` 目录级/根级调用返回 0 或被拒 → 臂被迫 128 文件逐个拉（128 页）才拿 4 行；一次根级聚合调用即可。
3. **L2.5b 种子文件自身公共面缺席**（TC c 段）：depth=N 只在消费文件面浮 def，种子文件（`__init__.py`）里的模块级 host hook（filemem_auto_retrieve/register L18208+/L18423）永远不出现在 hop-2——grep 臂靠精读抓到了。

**T-C 精确性主结论成立（源动下仍复现）**：索引 kwarg 84 行 = 正则 84 行逐字节一致；grep 101 行 = 84 语法 + 17 文本提及（.bak/文档/注释），方向与 L2.2 一致。
**口径备注**：源树在动（llm_caller.py 当日删、dirty_injection_sweep/sweep_dirty_injections/filemem_supersede 已被重构出 35000 行库——T-B 三族 miss 是 GT 漂移非桥 bug，重测需先锁源快照）；grep 臂背宿主钩子 attach（不变）；n=1/臂。

## L2.5 gate + live probe

- Reuses G1 (50 ms): the multi-hop expand is Python-side frontier
  iteration over point-lookup IN queries, no new gate.
- Live probe 2026-11-17 on filemem target (13.5k rows):
  - `min_score` depth=2 → 761 rows total (truncated=True, hop-1 dropped
    by the byte budget by design), 47 ms; entry-layer hop-2 defs include
    `FileMemProvider(MemoryProvider)`, `MemoryItem`, `MemoryTriggerRouter`.
  - `min_score` depth=3 → 1,531 rows, 37 ms; hop-3 call-side layer adds
    interior call sites (e.g. `self._store.search(...)`, `self._run_llm(...)`).
  - Byte-budget truncation regression: O(n²) single-line trims measured
    1,004 ms → 37/51 ms after binary-search trims (1,531-row / 761-row
    dense payloads).

## L2.3 wipe + rebuild campaign (2026-11-17)

- Full-volume call_site capture (10×): filemem target 1,933 → 19,645
  rows; kwarg 4,883 / parameter 2,883 byte-stable; +34 decorator /
  +4 extends rows; table 13,567 → 31,317 rows.
- Query still 18 ms under the 50 ms G1 gate after the 2.3× row growth.
- GT lesson: a regex count of `min_score(` returned 1 row — that row
  was a COMMENT, so the index correctly has 0. Always open the eyes on
  the ground truth, not just compare counts.

## L2.2 campaign (2026-11-17): 改 min_score 影响面，6 臂终局

- Task: "change `min_score` default 0.3 → 0.5 — enumerate the kwarg
  consumers + which tool entries are affected". Ground truth (owner,
  independent regex scan): 75 lines / 17 files; core = `__init__.py`
  only (3 kwarg lines); tests = 72 lines.

| arm | calls | wall | accuracy | notes |
|---|---|---|---|---|
| old index arm (no parameter rows) | 12 | 120 s | 75 ✓ | 0-hits → double grep |
| grep arm | 7 | 73 s | 73 ✗ (2 lines missed) | textual fast but undercounts |
| L2.1 arm (parameter rows) | 18 | 157 s | 75 ✓ + semantic note | declaration zero-grep; consumers still grep |
| **L2.2 arm (kwarg rows + paging)** | **15** | **35 s** | **76 ✓ full + per-file** | 5 cursor pages (22×3+10+exhaustion), zero grep primary |

The one failure mode (22/76 page misread) was closed at tool level:
`total` field (cdfd2bf) + disclosure paging discipline; the re-run
paged all 5 pages and matched disk 76 exactly.

## L2.1 campaign (2026-11-17): parameter rows land

- `min_score` 0 index lines → 2 declaration rows
  (`min_score: float = 0.3` @ L10114 / `= 0.5` @ L12556), sig carries
  the default. The old "0 hits → double grep" behavior is gone for
  declaration lookups; consumer enumeration still grep until L2.2.

## L2.0 campaign (2026-11-16, 30-file regime)

- grep arms beat index arms on small targets (3–4 s whole-tree scans);
  index wins reserved for the 30k-file regime and name/parameter regime
  (L2.2 proved the parameter side on the 127-file filemem target).
