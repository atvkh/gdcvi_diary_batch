# -*- coding: utf-8 -*-
"""实习系统「一次性批量提交日记」独立脚本(交互问答式)。

用法:
    python diary_batch_submit.py            交互式向导,全程确认后才提交
    python diary_batch_submit.py --dry-run  只生成内容+展示提交计划,绝不发提交请求

流程:读 config.json(LLM 配置+账号) -> 问答确定起止日期/星期规则/节奏
      -> 登录 -> 解析批次 pc -> 当月日历查重 -> LLM 按目标日期星期生成内容
      -> 展示计划 -> 手动输入 YES 确认 -> 分批+随机间隔逐篇提交
断点续跑:每篇结果即时写入同目录 diary_batch_state.json,重跑自动跳过已成功日期、只补失败项。

注意:日记无删除入口,提交即永久;未经当次确认脚本不会提交任何内容。
"""
import argparse
import json
import random
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import requests

# 打包成 exe 后 __file__ 指向临时解压目录,必须用 exe 所在目录定位配置文件
if getattr(sys, "frozen", False):
    SCRIPT_DIR = Path(sys.executable).parent
else:
    SCRIPT_DIR = Path(__file__).parent
STATE_FILE = SCRIPT_DIR / "diary_batch_state.json"

BASE_URL = "https://jw.gdcvi.edu.cn"
DIARY_ADD_URL = f"{BASE_URL}/sx/RjZjheYj/TjRj/"
DIARY_SAVE_URL = f"{BASE_URL}/sx/RjZjheYj/BcRj/r"
DIARY_VIEW_URL = f"{BASE_URL}/sx/RjZjheYj/CK/r"

WEEKDAY_NAMES = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")

DEFAULT_LLM_PROMPT = """你是一名大三全职顶岗实习的高职学生（人在校外，处于室内桌面办公或求职面试阶段）。请根据提供的【星期】，自选一个符合校外生活与办公实际的场景，生成一篇真实的实习日记。
【参考场景库（根据星期随机任选一个切入点）】
- 周一：开早会、整理电脑文件与文档分类、录入Excel数据、工位摸鱼、在招聘软件上筛选通勤方便/待遇合适的岗位投递简历
- 周二：主管或同事请喝奶茶/咖啡、学习办公软件与内部系统操作、打印复印装订资料、处理琐碎文档打杂
- 周三：在电脑前核对清单台账与数据报表、盯着屏幕眼睛发酸、去茶水间接水活动颈椎、工位日常摸鱼
- 周四：汇总日常数据、协助同事处理杂务、工位旁唠嗑吃零食、讨论下班吃什么、摸鱼等周末
- 周五：做完手头杂活整理桌面、清空电脑临时文件、倒计时等打卡下班、晚上和朋友聚餐吃火锅/烧烤
- 周六：睡到中午自然醒、洗衣服打扫住处房间、点外卖追剧、和朋友去商场逛街散心
- 周日：在住处宅着打游戏、刷招聘软件看新岗位与面试通知、整理下周待办、早睡迎接周一

【核心规则】
1. 真实口吻：语言生活化、接地气，符合校外实习/找工作年轻人的日常碎碎念与真实状态。
2. 严禁跑外勤：完全限定在室内办公、住处休息或求职状态，绝不出现任何工地、现场、出差等描述。
3. 严禁套话：严禁出现“受益匪浅”、“收获满满”、“在以后的工作中…”、“充实的一天”等虚假公文套话。
4. 严格遵循格式：直接输出以下两行纯文本，不要带有任何问候、Markdown 标记或解释说明：

TITLE: [4到10个字简短标题]
CONTENT: [60到100字的正文]

【输入】
今天是：{day_of_week}"""

# 法定节假日的专门场景提示词(仅当"节假日=注入节假日场景"时使用)
DEFAULT_HOLIDAY_PROMPT = """你是一名大三全职顶岗实习的高职学生(人在校外)。今天是法定节假日,你处于放假休息状态。请根据提供的【星期】,自选一个符合假期生活的真实场景,生成一篇实习日记。
【节假日场景库(任选一个切入点)】
- 睡到自然醒,在家刷手机、追剧、打游戏,点外卖解决三餐
- 假期回家与家人团聚,一起吃饭、聊天、帮忙做家务
- 白天出去逛逛商场、公园散心,或者约朋友短途出游
- 在家打扫住处、洗衣服、整理房间,给节后复工做准备
- 晚上和朋友聚餐,聊聊最近找实习、投简历的事
- 抽空刷招聘软件,看看新岗位和面试通知
【核心规则】
1. 真实口吻,语言生活化、接地气,符合放假年轻人的日常碎碎念。
2. 完全限定在休息/居家/短途休闲/求职状态,绝不出现任何上班、打卡、工地、现场、出差等描述。
3. 严禁套话:严禁"受益匪浅""收获满满""充实的一天"等虚假公文套话。
4. 严格遵循格式,直接输出以下两行纯文本,不要带任何问候、Markdown 标记或解释说明:

TITLE: [4到10个字简短标题]
CONTENT: [60到100字的正文]

【输入】
今天是：{day_of_week}"""

# 法定节假日(用于"跳过节假日/节假日场景"选项,日期请自行核实修改);同目录 holidays.txt 中每行一个 yyyy-MM-dd 会合并进来
HOLIDAYS = {
    # 2026 国庆(中秋为 9-25,如需连休自行追加)
    *[f"2026-10-{d:02d}" for d in range(1, 8)],
    # 2026 中秋
    "2026-09-25",
    # 2027 元旦
    "2027-01-01", "2027-01-02", "2027-01-03",
}

# 服务端提交响应判定关键字(已按 probe_capture.log 实测校准:成功="实习日记保存成功。",
# 重复="已存在指定日期的实习日记。")
SUCCESS_MARKERS = ("保存成功",)
DUPLICATE_MARKERS = ("已提交", "重复", "已存在", "只能添加")

DEFAULT_START = "2026-07-13"
DEFAULT_END = "2027-01-18"


def log(msg: str):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}")


class LoginFailedError(Exception):
    pass


def visible_text(html: str) -> str:
    txt = re.sub(r"(?is)<(script|style).*?</\1>", " ", html)
    txt = re.sub(r"(?s)<[^>]+>", " ", txt)
    return re.sub(r"\s+", " ", txt).strip()


def parse_date(s: str):
    try:
        return time.strptime(s.strip(), "%Y-%m-%d")
    except (ValueError, AttributeError):
        return None


def fmt(t: time.struct_time) -> str:
    return time.strftime("%Y-%m-%d", t)


def weekday_of(date_str: str) -> str:
    return WEEKDAY_NAMES[time.strptime(date_str, "%Y-%m-%d").tm_wday]


def load_holidays() -> set:
    days = set(HOLIDAYS)
    extra = SCRIPT_DIR / "holidays.txt"
    if extra.exists():
        for line in extra.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.split("#")[0].strip()
            if parse_date(line):
                days.add(line)
    return days


def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except Exception:
            log(f"警告: 状态文件 {STATE_FILE.name} 解析失败,将重建")
    return {"student_id": "", "results": {}}


def save_state(state: dict):
    STATE_FILE.write_text(
        json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def mark_result(state: dict, date_str: str, status: str, bt: str = "", detail: str = ""):
    state["results"][date_str] = {
        "status": status, "bt": bt,
        "detail": detail[:200], "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    save_state(state)


DONE_STATUSES = ("success", "duplicate")


def find_config() -> Path | None:
    candidates = [
        SCRIPT_DIR / "config.json",
        SCRIPT_DIR.parent / "gdcvi-checkin" / "config.json",
        SCRIPT_DIR.parent.parent / "gdcvi-checkin" / "config.json",
    ]
    for p in candidates:
        if p.exists():
            return p
    return None


def ask(prompt: str, default: str = "") -> str:
    suffix = f" [回车={default}]" if default else ""
    val = input(f"{prompt}{suffix}: ").strip()
    return val or default


def ask_int(prompt: str, default: int, lo: int, hi: int) -> int:
    while True:
        raw = ask(prompt, str(default))
        if raw.isdigit() and lo <= int(raw) <= hi:
            return int(raw)
        print(f"  请输入 {lo}~{hi} 的整数")


def ask_range(prompt: str, default: tuple, lo: int, hi: int) -> tuple:
    while True:
        d = f"{default[0]}-{default[1]}"
        raw = ask(f"{prompt}(格式 秒 或 秒下限-秒上限)", d)
        nums = re.findall(r"\d+", raw)
        if len(nums) == 1 and lo <= int(nums[0]) <= hi:
            return (int(nums[0]), int(nums[0]))
        if len(nums) == 2:
            a, b = sorted(int(x) for x in nums)
            if lo <= a and b <= hi and a <= b:
                return (a, b)
        print(f"  请输入 {lo}~{hi} 范围内的数值")


class Client:
    """会话封装:登录 + 自动提取令牌 + 会话失效时自动重登一次"""

    def __init__(self, student_id: str, password: str):
        self.student_id = student_id
        self.password = password
        self.s = requests.Session()
        self.s.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                          "AppleWebKit/537.36 (KHTML, like Gecko) "
                          "Chrome/150.0.0.0 Safari/537.36",
            "Referer": f"{BASE_URL}/sx/DL/Index",
        })
        self.xtcs = ""
        self.xtdm = ""
        self.name = ""

    def login(self):
        s = self.s
        r = s.get(f"{BASE_URL}/sx/DL/Index", timeout=15)
        if r.status_code != 200:
            raise RuntimeError(f"访问登录页失败: HTTP {r.status_code}")
        r = s.get(f"{BASE_URL}/sx/N/N1", timeout=15)
        nzm = s.cookies.get("r", "")
        if r.status_code != 200 or not nzm:
            raise RuntimeError("获取验证码失败")
        login_data = {
            "zdlx": "dn", "yhm": self.student_id, "mm": self.password,
            "Nzm": nzm, "btnLogin": "登录",
        }
        r = s.post(f"{BASE_URL}/sx/M/Sh", data=login_data,
                   allow_redirects=False, timeout=15)
        redirect = r.headers.get("Location", "")
        if r.status_code != 302 or "ZhuJMB010306" not in redirect \
                or not s.cookies.get(".ASPXAUTH"):
            raise LoginFailedError(
                f"登录被拒绝(HTTP {r.status_code}, 跳转: {redirect})——"
                "检查学号密码或稍后再试(连续错5次锁号5分钟)")
        r = s.get(f"{BASE_URL}/sx/ZhuJMB010306/ByJwb/", timeout=15)
        m_xtcs = re.search(r'name="Xtcs"\s+value="([^"]+)"', r.text)
        m_xtdm = re.search(r'name="Xtdm"\s+value="([^"]+)"', r.text)
        if not m_xtcs or not m_xtdm:
            raise RuntimeError("无法从首页提取 Xtcs/Xtdm")
        self.xtcs = m_xtcs.group(1)
        self.xtdm = m_xtdm.group(1)
        m_name = re.search(r"欢迎您\s*([^\s<]+)", r.text)
        if m_name:
            self.name = m_name.group(1).strip()

    def _looks_logged_out(self, r) -> bool:
        final_url = getattr(r, "url", "")
        return "/sx/DL/" in final_url

    def post(self, url: str, data: dict, timeout: int = 20):
        for attempt in (0, 1):
            try:
                r = self.s.post(url, data=data, timeout=timeout)
            except requests.RequestException as e:
                if attempt == 0:
                    log(f"网络异常({e}),重试...")
                    time.sleep(3)
                    continue
                raise
            if self._looks_logged_out(r) and attempt == 0:
                log("检测到会话失效,重新登录...")
                self.login()
                data.update(self.base_form())
                continue
            return r
        raise RuntimeError("会话恢复失败")

    def base_form(self) -> dict:
        return {"Xtcs": self.xtcs, "Xtu": self.student_id,
                "Xtsf": "xs", "Xtdm": self.xtdm}

    def fetch_pc(self) -> str:
        r = self.post(DIARY_ADD_URL, self.base_form())
        if r.status_code != 200:
            raise RuntimeError(f"日记表单页 HTTP {r.status_code}")
        # 先圈定 pcl 下拉框,再在框内优先取 selected option(selected/value
        # 属性顺序无关),无 selected 时兜底取第一个数字 value,避免取错批次
        m = re.search(r'name="pcl".*?</select>', r.text, re.DOTALL)
        if not m:
            raise RuntimeError("未从表单页解析到实习批次 pc")
        block = m.group(0)
        for tag in re.finditer(r'<option[^>]*>', block, re.IGNORECASE):
            attrs = tag.group(0)
            if re.search(r'\bselected\b', attrs, re.IGNORECASE):
                v = re.search(r'\bvalue="(\d+)"', attrs)
                if v:
                    return v.group(1)
        m2 = re.search(r'<option[^>]*\bvalue="(\d+)"', block, re.IGNORECASE)
        if m2:
            return m2.group(1)
        raise RuntimeError("未从表单页解析到实习批次 pc")

    def fetch_calendar_existing(self, pc: str) -> dict:
        """返回当月已有日记 {date_str: 详情页路径};只能可靠判断当前月份"""
        data = {**self.base_form(), "bt": "", "nr": "", "rq": "", "pc": pc}
        r = self.post(DIARY_VIEW_URL, data)
        if r.status_code != 200:
            raise RuntimeError(f"日历页 HTTP {r.status_code}")
        existing = {}
        for m in re.finditer(
                r'class="s_rja">\s*<a\s+title="(\d{4})年(\d{1,2})月(\d{1,2})日'
                r'[^"]*"[^>]*onclick="Xs\(\'([^\']+)\'\)"', r.text):
            y, mo, d, link = m.groups()
            existing[f"{y}-{int(mo):02d}-{int(d):02d}"] = link
        return existing

    def submit(self, bt: str, nr: str, pc: str, rq: str):
        """返回 (status, detail);status: success / duplicate / failed"""
        data = {**self.base_form(), "bt": bt, "nr": nr, "rq": rq, "pc": pc}
        try:
            r = self.post(DIARY_SAVE_URL, data)
        except requests.RequestException as e:
            return "failed", f"网络异常: {e}"
        if r.status_code != 200:
            return "failed", f"HTTP {r.status_code}"
        text = r.text.strip()
        if any(m in text for m in SUCCESS_MARKERS):
            return "success", ""
        vis = visible_text(text)
        if any(m in text or m in vis for m in DUPLICATE_MARKERS):
            return "duplicate", vis[:150]
        return "failed", vis[:200] or "(响应无可识别文本)"


def parse_llm_diary(text: str):
    if not text:
        return None
    m_bt = re.search(r"TITLE\s*[:：]\s*(.+)", text, re.IGNORECASE)
    m_nr = re.search(r"CONTENT\s*[:：]\s*(.+)", text, re.IGNORECASE | re.DOTALL)
    if m_bt and m_nr:
        bt = m_bt.group(1).strip().splitlines()[0].strip()
        nr = m_nr.group(1).strip()
        if bt and nr:
            return bt[:50], nr[:4000]
    m_json = re.search(r"\{.*\}", text, re.DOTALL)
    if m_json:
        try:
            data = json.loads(m_json.group(0))
            bt = str(data.get("bt") or data.get("title") or "").strip()
            nr = str(data.get("nr") or data.get("content") or "").strip()
            if bt and nr:
                return bt[:50], nr[:4000]
        except Exception:
            pass
    return None


def generate_content(llm_cfg: dict, date_str: str, retries: int = 3,
                     is_holiday: bool = False):
    """按目标日期的星期生成日记;法定节假日(is_holiday)用假期场景提示词。
    返回 (标题, 正文);失败返回 None"""
    api_base = (llm_cfg.get("api_base") or "").strip().rstrip("/")
    api_key = (llm_cfg.get("api_key") or "").strip()
    model = (llm_cfg.get("model") or "").strip()
    if not (api_base and api_key and model):
        raise RuntimeError("LLM 配置不完整(api_base/api_key/model)")
    prompt = (llm_cfg.get("prompt") or "").strip() or DEFAULT_LLM_PROMPT
    wd = weekday_of(date_str)
    if is_holiday:
        # 法定节假日用专门的假期场景提示词,不沿用工作日人设
        prompt = DEFAULT_HOLIDAY_PROMPT
        day_label = f"{wd}(法定节假日)"
    else:
        day_label = wd
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": prompt.replace("{day_of_week}", day_label)},
            {"role": "user", "content": f"今天是:{day_label},日期是:{date_str}"},
        ],
        "temperature": 1.0,
    }
    timeout = int(llm_cfg.get("timeout_sec") or 60)
    last_err = None
    for i in range(retries):
        try:
            r = requests.post(
                f"{api_base}/chat/completions", json=payload,
                headers={"Authorization": f"Bearer {api_key}"}, timeout=timeout)
            r.raise_for_status()
            text = r.json()["choices"][0]["message"]["content"]
            parsed = parse_llm_diary(text)
            if not parsed:
                raise ValueError(f"输出无法解析: {text[:120]}")
            return parsed
        except Exception as e:
            last_err = e
            if i < retries - 1:
                time.sleep(2 * (i + 1))
    log(f"  LLM 生成失败({date_str}): {last_err}")
    return None


def gen_batch_concurrent(gen_todo, plan, contents, failed_gen, state):
    """线程池并发生成一批;Ctrl+C 取消排队任务立即返回(已完成的保留)"""
    workers = plan.get("gen_workers", 4)
    done = 0
    ex = ThreadPoolExecutor(max_workers=workers)
    try:
        futs = {}
        for ds in gen_todo:
            is_holiday = (plan["holiday_mode"] == "scene") \
                and (ds in plan["holidays"])
            futs[ex.submit(generate_content, plan["llm"], ds,
                           is_holiday=is_holiday)] = ds
        for fut in as_completed(futs):
            ds = futs[fut]
            try:
                got = fut.result()
            except Exception:
                got = None
            done += 1
            if got:
                contents[ds] = got
                print(f"  [生成 {done}/{len(gen_todo)}] {ds} {weekday_of(ds)} "
                      f"《{got[0]}》 {got[1][:24]}...")
            else:
                failed_gen.append(ds)
                mark_result(state, ds, "failed", detail="LLM生成失败")
    except KeyboardInterrupt:
        ex.shutdown(wait=False, cancel_futures=True)
        print(f"\n(生成被中断,本批已完成 {done}/{len(gen_todo)},"
              f"未完成的下次重跑自动补)")
        raise
    ex.shutdown(wait=True)


WEEKDAY_MODES = {1: "每天", 2: "仅工作日", 3: "到周六"}
HOLIDAY_MODES = {1: "write", 2: "skip", 3: "scene"}
HOLIDAY_MODE_LABELS = {1: "照常写", 2: "跳过", 3: "注入节假日场景"}


def parse_range_arg(s):
    """解析 '20-60' 或 '20' 为 (lo, hi);None/无效返回 None"""
    if not s:
        return None
    nums = re.findall(r"\d+", s)
    if len(nums) == 1:
        return (int(nums[0]), int(nums[0]))
    if len(nums) == 2:
        a, b = sorted(int(x) for x in nums)
        return (a, b)
    return None


_instance_mutex = None


def acquire_single_instance() -> bool:
    """Windows 命名 mutex 单实例锁;进程结束系统自动释放,不占端口"""
    global _instance_mutex
    if sys.platform != "win32":
        return True
    import ctypes
    k32 = ctypes.windll.kernel32
    k32.CreateMutexW.restype = ctypes.c_void_p
    _instance_mutex = k32.CreateMutexW(
        None, False, "diary_batch_submit_single_instance")
    # ERROR_ALREADY_EXISTS = 183,表示已有实例持有同名 mutex
    return k32.GetLastError() != 183


def parse_args():
    parser = argparse.ArgumentParser(
        description="实习日记批量提交(配置驱动,参考 gdcvi_checkin 子命令架构)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "首次运行:    python diary_batch_submit.py --init\n"
            "测试内容:    python diary_batch_submit.py --test [--date 2026-08-26]\n"
            "正式批量:    python diary_batch_submit.py [--dry-run] [--start ... --end ...]\n"
        ),
    )
    parser.add_argument("--init", action="store_true",
                        help="交互引导配置 config.json(账号/llm/batch)")
    parser.add_argument("--test", action="store_true",
                        help="测试 LLM 生成 1 篇(不登录不提交,看质量)")
    parser.add_argument("--date", metavar="YYYY-MM-DD",
                        help="--test 指定日期(默认 batch.start)")
    parser.add_argument("--dry-run", action="store_true",
                        help="正式流程到预览统计后退出,不提交")
    parser.add_argument("--student-id", help="覆盖 config 学号")
    parser.add_argument("--start", help="覆盖起始日期")
    parser.add_argument("--end", help="覆盖结束日期")
    parser.add_argument("--weekday", type=int, choices=[1, 2, 3],
                        help="1=每天 2=仅工作日 3=到周六")
    parser.add_argument("--holiday", type=int, choices=[1, 2, 3],
                        help="1=照常 2=跳过 3=节假日场景")
    parser.add_argument("--batch-size", type=int, help="每批篇数")
    parser.add_argument("--gap", help="篇间隔秒(如 20-60)")
    parser.add_argument("--pause", help="批间停顿秒(如 90-180)")
    parser.add_argument("--workers", type=int,
                        help="并发生成线程数(默认 4)")
    return parser.parse_args()


def load_config_full():
    """返回 (cfg, cfg_path);无配置返回 (None, None)"""
    cfg_path = find_config()
    if not cfg_path or not cfg_path.exists():
        return None, None
    try:
        loaded = json.loads(cfg_path.read_text(encoding="utf-8-sig"))
        if not isinstance(loaded, dict):
            return None, cfg_path
        return loaded, cfg_path
    except Exception:
        return None, cfg_path


def build_plan_from_config(args):
    """读 config + 命令行覆盖 -> plan dict;缺配置返回 None(已打印提示)"""
    cfg, cfg_path = load_config_full()
    if cfg is None or not cfg.get("accounts") or not cfg.get("batch"):
        print("[!] 未检测到 config.json 或 accounts/batch 段,请先运行: "
              "python diary_batch_submit.py --init")
        return None
    acc0 = cfg["accounts"][0]
    student_id = (args.student_id or acc0.get("student_id") or "").strip()
    password = (acc0.get("password") or "").strip()
    if not student_id or not password:
        print("[!] config 账号不完整(student_id/password),请 --init 补全")
        return None
    llm_cfg = dict(cfg.get("llm") or {})
    llm_cfg.setdefault("prompt", DEFAULT_LLM_PROMPT)
    if not all((llm_cfg.get("api_base"), llm_cfg.get("api_key"),
                llm_cfg.get("model"))):
        print("[!] config llm 配置不完整,请 --init 补全")
        return None
    batch = cfg["batch"]
    start = args.start or batch.get("start") or DEFAULT_START
    end = args.end or batch.get("end") or DEFAULT_END
    if not (parse_date(start) and parse_date(end)):
        print(f"[!] 日期无效 start={start} end={end},请 --init 修正")
        return None
    weekday_mode = args.weekday or batch.get("weekday_mode", 1)
    holiday_mode = args.holiday or batch.get("holiday_mode", 3)
    batch_size = args.batch_size or batch.get("batch_size", 10)
    gap = parse_range_arg(args.gap) or tuple(batch.get("gap") or (20, 60))
    pause = parse_range_arg(args.pause) or tuple(batch.get("pause") or (90, 180))
    gen_workers = args.workers or batch.get("gen_workers", 4)
    gen_workers = max(1, min(int(gen_workers), 8))
    holidays = load_holidays()
    return {
        "cfg_path": str(cfg_path), "student_id": student_id,
        "password": password, "llm": llm_cfg,
        "start": fmt(parse_date(start)), "end": fmt(parse_date(end)),
        "skip_weekend": weekday_mode != 1,
        "weekend_max": 5 if weekday_mode == 2 else 6,
        "holiday_mode": HOLIDAY_MODES.get(holiday_mode, "scene"),
        "holidays": holidays, "batch_size": batch_size,
        "gap": tuple(gap), "pause": tuple(pause),
        "gen_workers": gen_workers,
        "dry_run": args.dry_run,
    }


def run_init():
    """--init: 交互引导收集 accounts/llm/batch,写入 config.json"""
    print("=" * 62)
    print("       实习日记批量提交 - 配置引导(--init)")
    print("=" * 62)
    cfg_path = find_config() or (SCRIPT_DIR / "config.json")
    cfg = {"version": 2, "accounts": [{}],
           "llm": {"api_base": "", "api_key": "", "model": "",
                   "prompt": "", "timeout_sec": 60},
           "batch": {"start": DEFAULT_START, "end": DEFAULT_END,
                     "weekday_mode": 1, "holiday_mode": 3,
                     "batch_size": 10, "gap": [20, 60], "pause": [90, 180]}}
    if cfg_path.exists():
        try:
            loaded = json.loads(cfg_path.read_text(encoding="utf-8-sig"))
            if isinstance(loaded, dict):
                # 保留旧值作默认,缺的段补上
                for k in ("llm", "batch"):
                    loaded.setdefault(k, cfg[k])
                if "accounts" not in loaded or not loaded["accounts"]:
                    loaded["accounts"] = cfg["accounts"]
                cfg = loaded
                print(f"  已加载现有配置 {cfg_path}(回车保留旧值)")
        except Exception as e:
            print(f"  旧配置解析失败({e}),从头配置")
    llm0 = cfg["llm"]
    llm0["api_base"] = ask("  LLM api_base(OpenAI兼容)",
                           llm0.get("api_base", "")).rstrip("/")
    llm0["api_key"] = ask("  LLM api_key", llm0.get("api_key", ""))
    llm0["model"] = ask("  LLM model", llm0.get("model", ""))
    acc0 = cfg["accounts"][0]
    acc0["student_id"] = ask("  学号", acc0.get("student_id", ""))
    acc0["password"] = ask("  密码(明文显示便于核对)",
                            acc0.get("password", ""))
    b = cfg["batch"]
    b["start"] = ask_date_input("  批次开始日期", b.get("start", DEFAULT_START))
    b["end"] = ask_date_input("  批次结束日期", b.get("end", DEFAULT_END))
    print("  写哪些天: 1=每天(含周末) 2=仅工作日 3=周一~周六")
    b["weekday_mode"] = ask_int("  选择", b.get("weekday_mode", 1), 1, 3)
    print("  节假日: 1=照常写 2=跳过 3=注入节假日场景")
    b["holiday_mode"] = ask_int("  选择", b.get("holiday_mode", 3), 1, 3)
    b["batch_size"] = ask_int("  每批篇数", b.get("batch_size", 10), 1, 500)
    b["gap"] = list(ask_range("  篇间隔随机区间(秒)",
                              tuple(b.get("gap", [20, 60])), 1, 3600))
    b["pause"] = list(ask_range("  批间停顿(秒)",
                                tuple(b.get("pause", [90, 180])), 1, 7200))
    if not (llm0["api_base"] and llm0["api_key"] and llm0["model"]
            and acc0["student_id"] and acc0["password"]):
        print("  配置不完整,已取消")
        return
    try:
        cfg_path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2),
                            encoding="utf-8")
        print(f"\n  已保存到 {cfg_path}")
        print("  下一步:")
        print("    python diary_batch_submit.py --test            (测1篇质量)")
        print("    python diary_batch_submit.py --dry-run         (预览不提交)")
        print("    python diary_batch_submit.py                   (正式批量)")
    except Exception as e:
        print(f"  保存失败({e})")


def test_content(args):
    """--test: 调 LLM 生成 1 篇,打印完整标题+正文,不登录不提交。
    只需 llm 段即可跑(batch 段缺则用默认日期/节假日模式)"""
    cfg, _ = load_config_full()
    if cfg is None or not cfg.get("llm"):
        print("[!] 未检测到 config.json 或 llm 段,请先: "
              "python diary_batch_submit.py --init")
        return
    llm_cfg = dict(cfg.get("llm") or {})
    llm_cfg.setdefault("prompt", DEFAULT_LLM_PROMPT)
    if not all((llm_cfg.get("api_base"), llm_cfg.get("api_key"),
                llm_cfg.get("model"))):
        print("[!] llm 配置不完整,请 --init 补全")
        return
    batch = cfg.get("batch") or {}
    date_str = args.date or batch.get("start") or DEFAULT_START
    if not parse_date(date_str):
        print(f"[!] --date 日期无效: {date_str}")
        return
    holidays = load_holidays()
    is_holiday = (batch.get("holiday_mode", 3) == 3) and (date_str in holidays)
    print(f"日期: {date_str} {weekday_of(date_str)}"
          + (" (法定节假日,用假期场景提示词)" if is_holiday else ""))
    print("-" * 40)
    got = generate_content(llm_cfg, date_str, is_holiday=is_holiday)
    if got:
        print(f"标题: {got[0]}")
        print(f"正文: {got[1]}")
    else:
        print("[!] 生成失败,检查 llm 配置或网络")
    print("-" * 40)
    print("(temperature=1.0,每次结果不同,可重复 --test 看随机性)")


def ask_date_input(prompt: str, default: str) -> str:
    while True:
        raw = ask(prompt, default)
        t = parse_date(raw)
        if t:
            return fmt(t)
        print("  日期格式应为 yyyy-MM-dd,例如 2026-07-13")


def build_dates(plan: dict) -> list:
    start = parse_date(plan["start"])
    end_ts = time.mktime(parse_date(plan["end"]))
    dates, ts = [], time.mktime(start)
    while ts <= end_ts:
        t = time.localtime(ts)
        ds = fmt(t)
        ok = True
        if plan["skip_weekend"] and t.tm_wday >= plan["weekend_max"]:
            ok = False
        if plan["holiday_mode"] == "skip" and ds in plan["holidays"]:
            ok = False
        if ok:
            dates.append(ds)
        ts += 86400
    return dates


def show_main_menu(plan: dict) -> str:
    """双击 exe(无参)时弹主菜单;返回 init/test/run/exit"""
    print("=" * 50)
    print("  实习日记批量提交 - 主菜单")
    print("=" * 50)
    print("  1. 修改配置(进 --init 引导)")
    print("  2. 测试生成 1 篇(不提交)")
    print(f"  3. {'dry-run 预演(不提交)' if plan.get('dry_run') else '正式批量提交'}")
    print("  4. 退出")
    print("-" * 50)
    print("  (命令行 --init/--test/--dry-run 可跳过本菜单)")
    while True:
        try:
            c = input("请选择 [3]: ").strip()
        except EOFError:
            c = "3"
        if c in ("", "3"):
            return "run"
        if c == "1":
            return "init"
        if c == "2":
            return "test"
        if c == "4":
            return "exit"
        print("  无效,请输入 1-4")


def main():
    if not acquire_single_instance():
        print("[!] 已有 diary_batch_submit 实例在运行,请勿重复启动。")
        print("    (若确认无其他实例,等几秒端口释放后重试)")
        try:
            input("按回车键退出...")
        except EOFError:
            pass
        return
    args = parse_args()
    if args.init:
        run_init()
        return
    if args.test:
        test_content(args)
        return
    plan = build_plan_from_config(args)
    if plan is None:
        run_init()
        plan = build_plan_from_config(args)
        if plan is None:
            return
    # 主菜单循环:改配置/测试后回菜单,选正式才离开
    while True:
        action = show_main_menu(plan)
        if action == "exit":
            return
        if action == "init":
            run_init()
            plan = build_plan_from_config(args)
            if plan is None:
                return
            continue
        if action == "test":
            test_content(args)
            continue
        break
    state = load_state()
    if state.get("student_id") and state["student_id"] != plan["student_id"]:
        state = {"student_id": plan["student_id"], "results": {}}
        log("状态文件属于其他学号,已重置")
    state["student_id"] = plan["student_id"]

    client = Client(plan["student_id"], plan["password"])
    log("登录中...(登录失败不可重试,防锁号)")
    try:
        client.login()
    except LoginFailedError as e:
        log(f"登录失败: {e}")
        sys.exit(1)
    except (RuntimeError, requests.RequestException) as e:
        log(f"登录网络/服务异常: {e}")
        sys.exit(1)
    who = f"{client.name}({plan['student_id']})" if client.name \
        else plan["student_id"]
    log(f"登录成功: {who},解析实习批次...")
    try:
        pc = client.fetch_pc()
    except (RuntimeError, requests.RequestException) as e:
        log(f"解析实习批次失败: {e}")
        sys.exit(1)
    log(f"实习批次 pc={pc}")

    today = time.strftime("%Y-%m-%d")
    all_dates = build_dates(plan)
    results = state["results"]
    todo, already_done = [], []
    for ds in all_dates:
        st = results.get(ds, {}).get("status")
        if st in DONE_STATUSES:
            already_done.append(ds)
        else:
            todo.append(ds)

    log("获取当月日历查重(服务端仅能判断当月)...")
    calendar_existing = {}
    try:
        calendar_existing = client.fetch_calendar_existing(pc)
    except Exception as e:
        log(f"警告: 日历获取失败({e}),当月查重跳过")
    existed_in_cal = [ds for ds in todo if ds in calendar_existing]
    todo = [ds for ds in todo if ds not in calendar_existing]

    future = [ds for ds in todo if ds > today]
    print("\n" + "=" * 62)
    print(f"计划概览  范围 {plan['start']} ~ {plan['end']}")
    print(f"  符合规则的日期 : {len(all_dates)} 天")
    print(f"  本地已完成     : {len(already_done)} 天(自动跳过)")
    print(f"  日历已有日记   : {len(existed_in_cal)} 天(自动跳过)"
          + (f" -> {','.join(existed_in_cal)}" if existed_in_cal else ""))
    print(f"  本次待提交     : {len(todo)} 篇"
          f"(其中未来日期 {len(future)} 篇,已实测可提交)")
    print(f"  节奏           : 每批{plan['batch_size']}篇,篇间隔"
          f"{plan['gap'][0]}~{plan['gap'][1]}秒,批间停"
          f"{plan['pause'][0]}~{plan['pause'][1]}秒,"
          f"并发生成 {plan.get('gen_workers', 4)} 线程")
    print("=" * 62)

    contents = {}
    failed_gen = []
    results = state["results"]

    if not todo:
        log("没有待提交的日期(本地已完成或日历已有),退出")
        sys.exit(0)

    if plan["dry_run"]:
        # dry-run: 生成全部 + 统计,不提交(确认全量生成是否成功)
        log(f"[DRY-RUN] 开始生成 {len(todo)} 篇内容"
            f"(并发 {plan.get('gen_workers', 4)} 线程,不提交,Ctrl+C 可中断)...")
        try:
            gen_batch_concurrent(todo, plan, contents, failed_gen, state)
        except KeyboardInterrupt:
            pass
        print("-" * 62)
        log(f"生成完成: 成功 {len(contents)} 篇, 失败 {len(failed_gen)} 篇")
        print(f"\n[DRY-RUN] 已生成 {len(contents)} 篇内容,未提交。"
              "\n去掉 --dry-run 正式运行(分批生成+提交)。")
        sys.exit(0)

    # 正式模式: 分批流水线(生成N篇→提交N篇→循环)
    # 不再全量预览(--test 已验证质量),YES 一次后流水线自动跑
    print("\n!!! 日记没有删除入口,提交即永久 !!!")
    ans = input(f"确认分批生成并提交 {len(todo)} 篇?"
                f" 输入大写 YES 继续,其他任意键取消: ").strip()
    if ans != "YES":
        log("已取消,未提交任何内容")
        sys.exit(0)

    items = list(todo)
    total_batches = (len(items) + plan["batch_size"] - 1) // plan["batch_size"]
    ok_cnt = dup_cnt = fail_cnt = 0
    log(f"开始分批流水线: {len(items)} 篇,分 {total_batches} 批,"
        f"每批 {plan['batch_size']} 篇")
    try:
        for batch_idx in range(0, len(items), plan["batch_size"]):
            batch = items[batch_idx:batch_idx + plan["batch_size"]]
            batch_no = batch_idx // plan["batch_size"] + 1
            log(f"=== 批次 {batch_no}/{total_batches} ===")

            # 1) 生成这批(线程池并发,跳过已生成/已完成,支持断点续跑)
            gen_todo = [ds for ds in batch
                        if ds not in contents
                        and results.get(ds, {}).get("status") not in DONE_STATUSES]
            if gen_todo:
                log(f"  并发生成 {len(gen_todo)} 篇"
                    f"({plan.get('gen_workers', 4)} 线程)...")
                gen_batch_concurrent(gen_todo, plan, contents,
                                     failed_gen, state)

            # 2) 提交这批(跳过生成失败/已完成)
            sub_todo = [ds for ds in batch
                        if ds in contents
                        and results.get(ds, {}).get("status") not in DONE_STATUSES]
            if sub_todo:
                log(f"  提交 {len(sub_todo)} 篇")
            for k, ds in enumerate(sub_todo, 1):
                bt, nr = contents[ds]
                status, detail = client.submit(bt, nr, pc, ds)
                mark_result(state, ds, status, bt=bt, detail=detail)
                tag = {"success": "OK ", "duplicate": "DUP", "failed": "ERR"}[status]
                print(f"  [提交 {k}/{len(sub_todo)}] {tag} {ds} 《{bt}》"
                      + (f" {detail}" if detail else ""))
                if status == "success":
                    ok_cnt += 1
                elif status == "duplicate":
                    dup_cnt += 1
                else:
                    fail_cnt += 1
                if k < len(sub_todo):
                    wait = random.uniform(*plan["gap"])
                    time.sleep(wait)

            # 3) 批间停顿
            if batch_idx + plan["batch_size"] < len(items):
                wait = random.uniform(*plan["pause"])
                log(f"  批间停顿 {wait:.0f}s...")
                time.sleep(wait)
    except KeyboardInterrupt:
        print("\n用户中断,已提交结果均已保存,可直接重跑续传")
    finally:
        save_state(state)

    print("=" * 62)
    print(f"完成: 成功 {ok_cnt} | 服务端判重复 {dup_cnt} | 失败 {fail_cnt}"
          f" | 生成失败 {len(failed_gen)}")
    if fail_cnt or failed_gen:
        print("失败日期已记录在状态文件,直接重跑本脚本即可只补失败项")
    print(f"状态文件: {STATE_FILE}")


if __name__ == "__main__":
    main()
