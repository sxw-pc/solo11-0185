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

独立入口：origin-import、test-import、string-import、alarm-import。每项导入格式为 `--db state.sqlite --file fixtures/<name>.json`。同源重复幂等，异内容复用事件 ID 则整批回滚。`audit` 在空库重放原始文件并比对业务状态。

alarm-import 采入 `alarm_capture`（波形段、设备钟校准、目击人、采样摘要）与 `alarm_link`（人工关联签认）两类事件，按 180 秒全局时间窗把 confirmed 告警编成最少事故单；迟到采样或签认变化只追加 incident_graph 新版本，已签旧图不改写。
