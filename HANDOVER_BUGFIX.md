# 交接文档:diary-batch 批量日记脚本 bug 修复交接

> 写给零上下文的新会话。本文档是唯一事实来源,与你的记忆/假设冲突时以本文档为准。
> 上一会话已完成一轮完整代码审查并修复 6 处,但**修复未全部收尾**(见第 5 节),且 **exe 是旧代码打的**。

---

## 1. 项目背景与文件位置

工作目录:`D:\Saul\desk\实习打卡\`,本任务涉及 `diary-batch\` 子目录:

| 文件 | 说明 |
|---|---|
| `diary_batch_submit.py` | 主脚本(约 690 行)。交互向导式批量提交实习日记,LLM 生成内容 |
| `capture_probe.py` | 抓包探测工具(需 mitmproxy,开发用,**不打包**) |
| `dist\diary_batch_submit.exe` | PyInstaller 打包产物(**当前是旧代码,见 P1**) |
| `probe_capture.log` / `probe_flows.mitm` | 前期抓包日志,本文档所有"实测"结论的出处 |
| `config.json` | 配置(LLM + 账号)。可能不存在,首次运行向导会创建 |
| `diary_batch_state.json` | 断点续传状态,运行后生成 |
| `holidays.txt` | 可选,每行一个 `yyyy-MM-dd` 补充节假日 |

同仓库 `..\gdcvi-checkin\gdcvi_checkin.py` 是主打卡脚本,本脚本的登录流程与之逐行一致,可作参照,但**不要改它**。

## 2. 脚本工作流程(理解用,不需要动)

```
向导[1-6]:config(有则确认/无则首次配置引导并保存) → 学号密码 → 日期范围
        → 星期规则(每天/仅工作日/到周六) → 节假日三选一(照常/跳过/注入假期场景,默认3)
        → 提交节奏(批大小/篇间隔/批间停顿)
→ 登录(失败立即退出,绝不重试) → fetch_pc 解析实习批次 → 当月日历查重
→ LLM 逐篇生成(星期按目标日期 rq 计算,节假日用内置假期提示词)
→ 全量预览 → dry-run 到此为止 / 正式模式输入大写 YES 才提交
→ 分批 + 随机间隔逐篇 POST,每篇结果即时写 state,重跑自动跳过 success/duplicate
```

`--dry-run` 参数:只生成+预览不提交(exe 双击无法传参,但到 YES 确认步不输 YES 同样安全)。

## 3. 已验证事实(实测校准过,**勿改判定逻辑、勿重复探测**)

以下全部来自真实抓包 `probe_capture.log`,已逐项核对:

1. 提交成功响应 = `实习日记保存成功。` → `SUCCESS_MARKERS = ("保存成功",)` ✓
2. 重复提交响应 = `已存在指定日期的实习日记。`(实测 3 次一致)→ `DUPLICATE_MARKERS` 含 `"已存在"` ✓。**服务端按日期拒重,不会产生重复日记**
3. 未来日期可提交(rq=2026-08-26 实测成功)——批量方案成立的前提
4. 批次解析:真实表单 `<option selected="selected" value="63">`,`fetch_pc()` 正则 ✓
5. 日历条目 `class="s_rja"><a title="2026年8月26日 星期三" onclick="Xs('...')"`,`fetch_calendar_existing()` 正则 ✓
6. 服务端响应全部 `charset=utf-8`,`r.text` 无编码问题
7. 提交字段 `Xtcs/Xtu/Xtdm/bt/nr/rq/pc` 与浏览器完全一致;脚本多带 `Xtsf=xs`,服务端忽略多余字段(无害)
8. 星期计算 `weekday_of()` 按 rq 日期的 `tm_wday`,与真实日历核对一致(08-24=周一、08-26=周三)
9. 登录 7 步(GET登录页→GET验证码拿Cookie r→POST登录→验302+.ASPXAUTH→首页提令牌)与主脚本一致
10. `Xtdm` 每次登录变化(实测同日两次登录分别为 82081 / 57019)

## 4. 已修复 bug(源码已改,**勿重修**;行号为写文档时快照,可能漂移)

| # | 问题 | 修法 |
|---|---|---|
| F1 | **[1/6] 回车确认 bug(关键)**:`ask(..., "回车确认")` 把"回车确认"当默认值,回车后返回值不在 `""/"y"/"yes"` 里,被当成路径 `Path("回车确认")` → 文件不存在 → 掉进首次配置 → 配置存进垃圾文件名 | 去掉默认值,回车=确认(约 401-405 行) |
| F2 | config.json 顶层非 dict(数组/字符串)时后续 `cfg.get` 崩溃且报错难懂 | 加 `isinstance(loaded, dict)` 校验(约 414-416 行) |
| F3 | llm 配置手动补全后不回存,每次运行重输 | 补全后写回 config.json(约 453-460 行) |
| F4 | [2/6] 改学号时仍沿用 config 里旧账号的密码 → 登录必失败 | 学号与 config 不一致时强制重输密码(约 467-469 行) |
| F5 | holidays.txt 严格 utf-8 读取,GBK 保存的文件直接崩(PowerShell 默认 GBK,项目已知坑) | `errors="replace"`(约 133 行) |
| F6 | 节假日计数文案重复歧义 | 文案修正(约 487 行) |

以上修复已通过 `py_compile`,**但 exe 未包含**(见 P1)。

## 5. 待修复问题(本次任务,按优先级)

### P0(真 bug,必修):`Client.post()` 重登后令牌过期
位置:`diary_batch_submit.py` 约 253-268 行(当前 `def post` 在 253 行,`self.login()` 在 265 行)。

问题:会话失效自动重登后,`self.login()` 更新了 `self.xtcs/self.xtdm`(每次登录令牌都变,见第 3 节第 10 条),但**重试的 `self.s.post(url, data=data, ...)` 仍用调用前快照的旧令牌** → 重试大概率被拒,整个"会话失效自动重登"特性形同虚设(表现为长批次跑到一半全部标 failed)。

建议修法(最小改动):`self.login()` 之后、`continue` 之前刷新令牌字段:

```python
self.login()
data.update(self.base_form())   # base_form 只有 Xtcs/Xtu/Xtsf/Xtdm,不会碰 bt/nr/rq/pc
continue
```

### P1(收尾,必做):重新打包 exe
`dist\diary_batch_submit.exe`(13:25)早于最后一批源码修复(13:36),**不含第 4 节的修复,也不含 P0**。P0 修完后必须重新打包(命令见第 7 节),并确认 exe 时间戳新于源码。

### P2(可选,低优先):`find_config()` exe 路径回退失效
位置:约 165-173 行。回退候选 `SCRIPT_DIR.parent / "gdcvi-checkin" / "config.json"` 只在源码运行时命中(此时 SCRIPT_DIR=diary-batch\,parent=实习打卡\)。exe 在 `dist\` 下运行时 parent 是 diary-batch\,找不到主配置。可追加候选 `SCRIPT_DIR.parent.parent / "gdcvi-checkin" / "config.json"`(源码运行时该路径不存在,无害;exe 运行时正好命中 实习打卡\gdcvi-checkin\)。非崩溃问题(首次配置向导兜底),可修可不修。

## 6. 已知"非 bug"(设计取舍,**勿顺手改**)

- 日历查重只能查当月(服务端限制),跨月靠本地 state + 服务端拒重兜底(实测安全)
- 节假日模式(选 3)下 `generate_content` **强制用内置 `DEFAULT_HOLIDAY_PROMPT`**,忽略用户自定义 prompt——设计如此
- 提交带 `Xtsf=xs` 多余字段——无害,保留
- LLM 生成成功但提交阶段中断时,内容不持久化,重跑重新生成(接受 LLM 重跑成本)
- 密码明文显示、明文存 config.json——项目约定(掩码输入曾导致输错无法核对,已刻意移除)
- `DONE_STATUSES = ("success", "duplicate")`,duplicate 视为完成不重试
- Ctrl+C 处理、YES 确认、dry-run 机制——安全链,不可移除

## 7. 硬约束(必须遵守)

1. **禁止**实现任何突破提交/打卡次数限制的功能
2. **登录失败立即终止,绝不重试**(连续错 5 次锁号 5 分钟)——现有 `LoginFailedError` 语义不可改
3. `Xtdm/Xtcs` 必须每次登录动态提取,**禁止硬编码**
4. 日记**无删除入口,提交即永久**——dry-run 与大写 YES 确认机制不可移除或弱化
5. 文档/示例中**禁止出现真实学号、密码、地址**,一律用占位符(YourStudentID/YourPassword123)
6. 不改 `gdcvi-checkin\` 下的任何文件
7. Windows 环境:PowerShell 里 curl 是 Invoke-WebRequest 别名(用 curl.exe);PowerShell 默认 GBK 输出,跨进程读取加 `errors="replace"` 兜底

## 8. 验证与打包步骤

```powershell
cd D:\Saul\desk\实习打卡\diary-batch
# 语法校验(每次改完必跑)
python -m py_compile diary_batch_submit.py
# 重新打包(P0/P1 完成后)
python -m PyInstaller --onefile --console --name diary_batch_submit --collect-all requests diary_batch_submit.py --noconfirm
# 确认 exe 时间戳新于 .py
Get-Item diary_batch_submit.py, dist\diary_batch_submit.exe | Select-Object Name, LastWriteTime
```

注意:exe 是 onefile 模式,运行时 `__file__` 指向临时解压目录,脚本已用 `getattr(sys, "frozen", False)` 切到 `sys.executable` 所在目录定位 config/state/holidays——**这逻辑勿动**。exe 运行时 config.json 需放 exe 同目录(dist\)。

## 9. 交付标准

- P0 修复并解释修法;P1 重打包且时间戳验证通过;(可选)P2 说明做或不做的理由
- `py_compile` 通过;不引入新依赖;不碰第 3/6 节标注"勿改"的内容
- 改动最小化:只修列出的 bug,不顺手重构/加功能/加注释
