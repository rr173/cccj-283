# 离线档案引用迁移工具

用 Python 标准库实现的旧引用迁移工具。它只使用**已经核准的相邻版次位置对照表**，
不做 OCR、不做文本相似度猜测，也不根据相同页号、邻近页或内容外观补路径。

所有坐标都是从零开始的整数，区间一律为左闭右开 `[start, end)`。

## 1. 输入档案格式

一个档案 JSON 描述同一卷宗的版次、页和每两个相邻版次之间的对照表：

```json
{
  "editions": [
    {
      "id": "v1",
      "pages": [
        {"id": "p1", "length": 100}
      ]
    },
    {
      "id": "v2",
      "pages": [
        {"id": "a", "length": 200}
      ]
    }
  ],
  "tables": [
    {
      "from_edition": "v1",
      "to_edition": "v2",
      "mappings": [
        {
          "id": "m1",
          "from_page": "p1",
          "from_start": 0,
          "from_end": 40,
          "to_page": "a",
          "to_start": 100,
          "to_end": 140
        }
      ]
    }
  ]
}
```

要点：

- `editions` 按卷宗先后排列。
- 页 ID 只在所属版次内有意义；不同版次出现相同页 ID 不会自动产生对应关系。
- 每条 `mapping` 把旧页的 `[from_start, from_end)` 映射到新页的等长
  `[to_start, to_end)`，相对偏移保持不变。
- 多条 mapping 可以重叠。
- **提供了表但 `mappings: []`** 表示该交接没有任何核准对应，位置为“无去向”。
- **缺少整张表**不是空表；查询跨越该交接时报 `MissingTableError` / CLI 退出码 `2`。
- 非法页 ID、负坐标、`start > end`、区间超出页长、映射两端长度不等、
  对照编号重复等都会报错。

仓库内有完整示例：`example_archive.json`。

## 2. 预建磁盘索引

```bash
python3 -I /workspace/citation_tool.py build-index \
  /workspace/example_archive.json \
  /tmp/citation-index.sqlite \
  --force
```

可选参数：

- `--force`：目标索引已存在时，成功构建后原子替换。
- `--strict-tables`：只要整卷任意相邻版次缺表就令构建失败。默认允许先建索引，
  缺表会记录为 `table_present = 0`；真正有查询跨越时才报错。

索引是标准 SQLite 文件，复合索引为：

```sql
CREATE INDEX idx_mappings_overlap
ON mappings (edge_index, from_page, from_start, from_end);
```

单次查询对 mapping 表的访问类似：

```sql
SELECT ...
FROM mappings
WHERE edge_index = ?
  AND from_page = ?
  AND from_start < ?   -- 查询区间 end
  AND from_end > ?     -- 查询区间 start
```

因此只读取所选版次之间、指定页上与当前实际到达区间相交的对照，不扫描整卷。

### 构建/装载失败时的状态安全

新索引先写到同目录临时文件 `.<name>.*.building`，执行 schema、写入和
`PRAGMA integrity_check`，成功后才 `os.replace()` 原子替换目标文件。

- 构建失败：关闭并删除本次临时文件，旧索引不被修改。
- 重复构建失败：此前可查询的 SQLite 索引仍在原路径，可继续打开。
- 打开损坏文件：抛出 `IndexLoadError`，工具不会删除或重建该文件。

## 3. 查询旧引用

```bash
python3 -I /workspace/citation_tool.py query \
  /tmp/citation-index.sqlite \
  --source-edition v1 \
  --target-edition v3 \
  --page p1 \
  --start 0 \
  --end 100
```

也可用零基页序代替页 ID：

```bash
... query /tmp/citation-index.sqlite \
  --source-edition v1 --target-edition v3 \
  --page-index 0 --start 0 --end 100
```

JSON 输出：

```bash
python3 -I /workspace/citation_tool.py query \
  /tmp/citation-index.sqlite \
  --source-edition v1 --target-edition v3 \
  --page p1 --start 0 --end 100 \
  --format json
```

示例文本结果：

```text
旧引用 v1/p1:[0,100) -> 新版 v3
总体判定: 混合结果
长度统计: 唯一=90, 多处=10, 无去向=0
  [0,30) 唯一落点:
    -> f:[0,30)  凭据链: m1 -> m4
  [30,40) 多处落点:
    -> f:[30,40)  凭据链: m1 -> m4
    -> f:[100,110)  凭据链: m2 -> m5
  [40,70) 唯一落点:
    -> f:[110,140)  凭据链: m2 -> m5
  [70,100) 唯一落点:
    -> f:[40,70)  凭据链: m3 -> m4
```

`--fail-if-not-unique` 可用于脚本：当总体状态不是 `unique` 时以退出码 `4` 结束。

## 4. 返回结构

结果中的 `segments` 是对原始 `[start,end)` 的连续划分：

```json
{
  "interval": [30, 40],
  "status": "multiple",
  "destinations": [
    {
      "page": "f",
      "interval": [30, 40],
      "chain": ["m1", "m4"]
    },
    {
      "page": "f",
      "interval": [100, 110],
      "chain": ["m2", "m5"]
    }
  ]
}
```

状态含义：

- `unique`：该旧引用片段在目标版只有一个实际页/区间落点。
- `multiple`：有两个或更多不同的实际页/区间落点。
- `lost`：在所提供的对照链中没有任何完整路径到达目标版。

每个 destination 都带至少一条完整凭据链。该链中的每条对照都覆盖所报告的源区间
和落点区间；测试会重新应用链来验证这一点。

## 5. Python API

```python
import sys
sys.path.insert(0, "/workspace")

from citation_migration import IndexStore, MigrationEngine

with IndexStore.open("/tmp/citation-index.sqlite") as store:
    result = MigrationEngine(store).migrate(
        source_edition="v1",
        target_edition="v3",
        page_id="p1",
        start=30,
        end=40,
    )

print(result.overall_status)       # unique / multiple / lost / mixed
print(result.to_dict())
for segment in result.segments:
    print(segment.start, segment.end, segment.status)
    for dest in segment.destinations:
        print(dest.page_id, dest.start, dest.end, dest.chain)
```

## 6. 迁移算法的保证

1. **按区间处理，不逐位置循环。**  
   只在 mapping 边界、下游结果边界处切分整数区间。即使引用长度为十亿，只要对照
   连续，也只会产生少量区间。

2. **只沿相邻版次对照表前进。**  
   未在某一交接列出的位置立即成为该路径的终点；后续版次中相同页 ID、相同坐标或
   邻近内容不能使其复活。

3. **相同实际落点去重。**  
   多条路径若到达目标版同一页、同一精确区间，destination 字典只保留一处。

4. **中间多份不等于最终多处。**  
   中途可以有多个副本；算法继续计算各副本到目标版的结果。若只有一份保留，最终仍
   判为 `unique`。相邻片段若能由同一条完整链连续覆盖，会合并成更大的连续区间。

5. **不展开全部指数路径。**  
   下游计算以
   `(目标版, 当前版次, 页 ID, start, end)`
   为键做记忆化。汇聚到相同页/区间的多条进入路径共享同一次下游计算，而不是把每条
   完整中间路径分别 DFS 一遍。

6. **可核查。**  
   合并区间时会重新应用所选凭据链，确认它确实完整覆盖合并后的源区间并生成所报告的
   目标区间。

## 7. 运行测试

```bash
python3 -I /workspace/run_tests.py
```

测试只使用 Python 标准库；当前有 18 个测试方法，其中包含 120 组固定随机种子的
区间算法与逐位置暴力 oracle 对拍。覆盖：

- 唯一、多处、无去向和区间偏移；
- 部分重叠后的精确裁剪；
- 相同落点去重；
- 中途多份、后来仅一份保留；
- 缺表报错且不按删除处理；
- 空表与缺表的区别；
- 跨版相同页 ID 不产生路径；
- 非法坐标；
- SQLite 索引计划和最小重叠查询；
- 大区间不逐位置处理；
- 连续菱形汇聚的记忆化/防路径指数展开；
- 120 组随机档案与逐位置暴力 oracle 对拍；
- 失败重建不破坏旧索引；
- 损坏索引装载失败；
- 在 `python -I` 子进程中复跑 CLI。

## 8. 目录

```text
/workspace
├── citation_tool.py                 # CLI 入口（兼容 python -I）
├── run_tests.py                      # 可复跑测试入口
├── example_archive.json             # 示例档案
├── citation_migration/
│   ├── archive.py                   # JSON 读取与坐标/对照校验
│   ├── index.py                     # 原子 SQLite 磁盘索引
│   ├── engine.py                    # 区间迁移、去重、记忆化、凭据合并
│   ├── model.py                     # 数据结构
│   ├── errors.py
│   └── cli.py
└── tests/
    └── test_citation_migration.py
```
