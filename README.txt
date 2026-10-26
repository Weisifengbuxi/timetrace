时迹 TimeTrace v1.0.0
=====================

给 RustTimeNoter 装一条时间线：看清「几点几分在用什么软件」。

先决条件
--------
先装好 RustTimeNoter（本工具只读它的数据，不记录任何东西）。
下载：见 RustTimeNoter 的发布页。

快速开始
--------
1. 把这个文件夹解压到任意位置（不用管理员权限）
2. 双击「时间线.bat」，浏览器会打开时间线页面
3. 想查某个时刻 / 某个软件，双击「查时间.bat」/「查软件.bat」

四个入口
--------
时间线.bat     生成并打开时间线页面              TimeTrace.exe html
查时间.bat     输入 14:30，回答那一刻在用什么      TimeTrace.exe at "14:30"
查软件.bat     输入 pallas，查它什么时候被用过     TimeTrace.exe app pallas
当天记录.bat   把某一天打印成文字流水              TimeTrace.exe day --date 2026-10-26

页面上的按钮
------------
- 右上角「保存本页」：另存为独立的 HTML 报告（数据在文件里，自己保管）
- 「每天默认收起表格」：勾上后点日期那一行展开当天的完整会话表
- 「全部应用」：点任意应用可只看它；再点一次取消
- 输入 14:30 或 2026-10-26 14:30：直接跳到那一刻并高亮

保留 30 天
----------
每次运行都会先删除 30 天以前的日志分片（硬删除，不进回收站）。
只删 %LOCALAPPDATA%\RustTimeNoter\data 下形如 YYYY-MM-DD.log 的文件，
配置、密钥、字典和其它文件不动。
先看会删什么：  TimeTrace.exe prune --dry-run
想保留更久：    TimeTrace.exe prune --days 60

隐私
----
所有数据都在本机，页面是离线 HTML，不联网、不收集任何信息。
「保存本页」得到的文件包含你的完整操作记录，请自行保管。

常见问题
--------
* 提示找不到 tracker.exe：先安装 RustTimeNoter；或把 tracker.exe 放到本文件夹。
* 杀毒软件报毒：exe 由 PyInstaller 打包且未签名，可能误报。
  可以改用 Python 运行 timetrace.py，或用 build.ps1 自己重新打包。

许可：MIT
