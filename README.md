# 时迹 TimeTrace

**给 RustTimeNoter 装一条时间线：一眼看清「什么时候用了什么软件」。**

RustTimeNoter 记下了你每天的电脑使用情况，但它自带的报表只回答「某个应用一共用了多久」。
时迹读它的数据，把零散记录还原成一段段会话，生成一张**本地、离线、可搜索**的时间线：

- 24 小时色带：鼠标移上去就显示那一刻在用什么、这一段从几点到几点
- 每天一张卡片：应用合计 + 完整会话表，打开即全部展开
- 查某个软件：看到一个陌生进程名，输入几个字母就知道它什么时候跑过
- 查某个时刻：输入 `14:30`，直接跳到那一刻并高亮
- 保存成独立 HTML：一份报告一个文件，存档、发给自己都行

> 时迹**不是**数据采集器。记录原始数据的是 **RustTimeNoter**，本项目只是它的查看器，
> 详见文末[致谢](#致谢)。

![时迹界面](screenshot.png)

## 快速开始

1. 先装好 **RustTimeNoter**（本工具只读它的数据，不记录任何东西）
2. 下载 [`TimeTrace-1.0.1-win64.zip`](../../releases/download/v1.0.1/TimeTrace-1.0.1-win64.zip)（约 8.6 MB，或到 [Releases](../../releases) 看所有版本），解压到任意位置
3. 双击 **`时间线.bat`** —— 浏览器会打开时间线页面

不需要安装 Python、不需要管理员权限、不写注册表、不联网。

## 四个入口

| 双击 | 作用 | 等价命令 |
| --- | --- | --- |
| `时间线.bat` | 生成并打开时间线页面 | `TimeTrace.exe html` |
| `查时间.bat` | 输入 `14:30`，回答那一刻在用什么 | `TimeTrace.exe at "14:30"` |
| `查软件.bat` | 输入 `pallas`，回答这个程序什么时候被用过 | `TimeTrace.exe app pallas` |
| `当天记录.bat` | 把某一天打印成文字流水 | `TimeTrace.exe day --date 2026-10-26` |

## 时间线页面

打开后从上到下：

- **总览**：天数、总记录时长、会话数、应用数、平均每天
- **查应用**：每个应用的总时长、出现天数、首次使用、最近一次、段数；输入框过滤，点一行只看它
- **全部应用**：完整列出（不省略），点一下按应用过滤整页
- **跳到某天**：日期锚点
- **每天一张卡片**：24 小时色带 + 当天应用合计 + 当天完整会话表
- **收起 / 展开**：勾选「每天默认收起表格」后点日期那一行展开；也有全部展开 / 全部收起
- **保存本页**：把当前报告另存为独立 HTML（数据在内嵌在文件里）

色带里灰色斜纹 = 有记录但未归属到应用的空档；空白 = 完全没有记录（关机 / 睡眠 / 未运行）。
相邻的同一应用记录（间隔 ≤ 3 秒）已合并成一段会话。

## 保留 30 天，过期自动清理

- 每次运行都会先清理：删除 `%LOCALAPPDATA%\RustTimeNoter\data\YYYY\MM\` 下日期早于 30 天的 `YYYY-MM-DD.log`
- 只删严格匹配 `YYYY-MM-DD.log` 的分片；配置、密钥、字典和其它文件一律不碰
- 页面默认范围同样是最近 30 天
- 先看会删什么：`TimeTrace.exe prune --dry-run`
- 改保留期：`TimeTrace.exe prune --days 60` 或 `TimeTrace.exe html --retention 60`
- 临时不清理：`TimeTrace.exe html --no-prune`

> **注意：这是硬删除，不进回收站。** 想长期留存请用页面上的「保存本页」，或定期备份数据目录，或fork到自己仓库修改为你需要的范围。

## 命令行

```
TimeTrace.exe html                     生成时间线并打开浏览器
TimeTrace.exe html --days 7            只看最近 7 天
TimeTrace.exe html --from 2026-10-01 --to 2026-10-31
TimeTrace.exe html --out report.html --no-open
TimeTrace.exe at "14:30" --context 20  某一时刻 + 前后 20 分钟
TimeTrace.exe app pallas --gap 600     查应用；间隔 10 分钟内的使用合并成一个时段
TimeTrace.exe day --date 2026-10-26    某天的文字流水
TimeTrace.exe prune --dry-run          预演清理
TimeTrace.exe --version
```

`html` / `day` / `app` 都支持 `--days`、`--from`、`--to`、`--retention`、`--no-prune`。

## 隐私

- 数据始终在 `%LOCALAPPDATA%\RustTimeNoter`，时迹只读取此数据
- 页面是本地静态 HTML，数据内嵌其中，不会上传任何东西
- 本工具不收集遥测、不含网络请求

## 常见问题

**双击后提示「找不到 tracker.exe」**
说明还没装 RustTimeNoter，或者它装在非默认位置。先安装 RustTimeNoter；也可以把
`tracker.exe` 直接放到 TimeTrace.exe 旁边。

**杀毒软件报毒**
`TimeTrace.exe` 由 PyInstaller 打包，未签名，部分杀软会对这类 exe 误报。
源码就在仓库里，可以自己用 `build.ps1` 重新打包，或者直接用 Python 跑源码（见下）。

**为什么只有 30 天数据**
这是默认保留期，见上一节。想留更久就调大 `--days` / `--retention`，并定期「保存本页」或备份数据目录。

**时间线上的空白与灰色斜纹**
空白 = 那段时间完全没有记录（关机、睡眠、daemon 没运行）；灰色斜纹 = 有记录但没归属到应用的空档。

## 用 Python 直接运行（不打包）

需要 Python 3.9+，无第三方依赖：

```
python timetrace.py html
python timetrace.py app weixin
```

## 自己打包

```
powershell -ExecutionPolicy Bypass -File build.ps1
```

会调用 PyInstaller 生成 `dist\TimeTrace.exe`（需要 `pip install pyinstaller`）。

## 致谢

时迹是在别人的基础上做的：

- **RustTimeNoter** 是采集器。它负责把前台窗口、使用时长、AFK 判定这些
  原始数据记下来并加密存放在 `%LOCALAPPDATA%\RustTimeNoter`，
  只是读它导出的数据、把它变成看得懂的时间线。没有 RustTimeNoter，就没有这个页面。
  RustTimeNoter 及其版权归原作者所有。
- 感谢 RustTimeNoter 的作者把「按 UTC 日分片 + 加密日志 + 官方导出接口」这套东西做的十分完善，
  时迹才能只靠 `tracker export` 就拿到完整数据。
- 选择在**RustTimeNoter**优化是因为我很喜欢它的开机自启功能，如果能加个图标就更好了（后续优化方向）

RustTimeNoter 这个软件还是比较知名的，这里就不给出它的仓库地址了，所以这里以项目名致谢

## 许可

MIT，见 [LICENSE](LICENSE)。

时迹与 RustTimeNoter 作者无关，是社区写的第三方查看器；RustTimeNoter 的名称与代码版权归其作者。
