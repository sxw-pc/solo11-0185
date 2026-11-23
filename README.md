# 储能电池梯次利用采证台

来源回收、独立检测分选和组串投运三账联动，决定梯次利用资格与告警隔离范围。

Python 3.11+ / SQLite，合成数据离线运行。

```powershell
python -m pip install . --no-deps --no-build-isolation
python -m secondlife demo --db demo.sqlite
python -m secondlife report --db demo.sqlite
python -m secondlife audit --db demo.sqlite
python -m unittest discover -s tests -v
```

独立入口：origin-import、test-import、string-import。每项导入格式为 `--db state.sqlite --file fixtures/<name>.json`。同源重复幂等，异内容复用事件 ID 则整批回滚。`audit` 在空库重放原始文件并比对业务状态。

## 退役包拍卖分配台账

`auction-import` 采录离线加密报价文件，`auction-allocate` 产出覆盖全部挂牌包的
“包 → 有效中标方 → 金额”分配表，`auction-report` 查台账，`auction-audit` 空库重放原件比对。

```powershell
python -m secondlife auction-import --db auction.sqlite --file fixtures/auction-budget.json
python -m secondlife auction-allocate --db auction.sqlite
python -m secondlife auction-audit --db auction.sqlite
python -m pytest -q
```

采录与分配规则：

- **报价原字节留存**：加密文件原字节按 sha256 入库（`cipher_files`），解密与见证
  时间、买方身份、报价编号全程留痕，哈希不符整批回滚；报价用买方核额登记密钥做
  HMAC-SHA256 验签，伪签件拒绝但不污染标的。
- **跨标的共享保证金**：每买方中标价总额不得超过已核额度。逐包各取最高（`winners`）
  只作单包基线；双包共享额度时它会产生不可执行的 A/A（应付 160、额度仅 100）。
  `auction.allocate` 用按标的分支定界求全局可行最优解，输出每买方额度使用表。
- **唯一落槌与补传**：一包只能落一家；同 `bid_no` 补传留痕但不产生第二单。
- **已交割不可夺**：已交割包按历史事实冻结，迟到高价文件标记 `late` 并拒绝，
  迟到的撤签同样不改旧交割。
- **优先锁与撤签**：双签优先锁未撤签时标的只能给锁权方；撤签必须买方与站方共同
  签署，可把优先权迁至另一标的。同报价同额度下两张交割表总额相同但权属路线不同，
  总额报表不能替代交割表。
- **资格回待审**：来源或健康版次变化只把相应挂牌包置回待审，已揭标原件与事件版次保留。
- **可回放**：分配版次只追加；`auction-audit` 在空库重放全部原件并按相同版次重算，
  物化台账被篡改即可检出。

