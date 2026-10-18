# =====================================================================
# app.py  ——  IPTV 频道管理 Web 后台（Flask + SocketIO 版，逐行中文注释 + 优化）
# 功能概览：
#   1) 提供频道列表页面，解析 output/sctv.txt 展示单播(rtsp)/组播地址
#   2) 提供配置页面，读写 config/config.json
#   3) 手动触发后台更新脚本 get_iptv_channels_V1.1.py，并实时推送日志
#   4) 通过 crontab 管理定时更新任务（增/查/删）
#   5) 对外提供 output/ 目录下文件（如 epg.xml.gz、m3u）的静态访问
# 优化要点（详见各段注释）：
#   - 把所有 import 统一提到文件顶部（原代码把 threading/traceback 写在函数内部）
#   - 修复 run_script_with_logging 中 returncode 判断的 bug：join 线程后必须先
#     process.wait() 才能拿到退出码，否则 returncode 恒为 None，永远误判为"成功"
#   - 路径计算统一用 BASE_DIR 常量，避免多处 os.path.dirname(__file__) 重复
#   - 与 None 的比较统一用 is None / is not None
#   - 注释掉生产环境不宜保留的大段请求体调试 print（保留关键错误日志）
#   - 标注 CORS 配置中 origins="*" 与 supports_credentials=True 的冲突风险
# =====================================================================

# ---------- 第三方库导入 ----------
from flask import (            # 导入 Flask 核心及常用请求/响应工具
    Flask,                      # Flask：创建 Web 应用实例
    request,                    # request：读取 HTTP 请求里的表单/JSON/头等信息
    jsonify,                    # jsonify：把 Python 字典转成 JSON 响应返回
    redirect,                   # redirect：返回一个 302 重定向响应
    render_template,            # render_template：渲染 templates/ 下的 HTML 模板
    send_from_directory,        # send_from_directory：安全地按目录返回静态文件
)
from flask_cors import CORS                 # CORS：为接口开启跨域资源共享支持
from flask_socketio import SocketIO, emit   # SocketIO：WebSocket 服务端；emit：向客户端推送事件
import subprocess   # subprocess：在后台启动子进程（运行更新脚本、调用 crontab）
import json         # json：读写 config.json 配置文件
import os           # os：路径拼接、文件存在性判断、获取环境变量等
import time         # time：sleep，用于脚本启动前的短暂延时
import sys          # sys：sys.executable 取当前 Python 解释器路径
import threading    # threading：开子线程并行读取子进程的 stdout/stderr（原在函数内，已上提）
import traceback    # traceback：异常时打印完整堆栈（原在 except 内，已上提）


# ---------- 应用与全局对象初始化 ----------
# 创建 Flask 应用：static_folder 指定静态目录为 static，static_url_path 让其通过 /static 访问
app = Flask(__name__, static_folder="static", static_url_path="/static")

# 工程根目录（本文件所在目录）。所有相对脚本/配置的路径都基于它拼接，集中维护
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# 确保 logo 目录存在（send_from_directory 要求目录必须存在）
logo_dir = os.path.join(BASE_DIR, "logo")
if not os.path.exists(logo_dir):
    os.makedirs(logo_dir)
    print(f"[INFO] 已创建 logo 目录: {logo_dir}")
else:
    print(f"[INFO] logo 目录已存在: {logo_dir}")
    # 列出目录中的文件便于调试
    logo_files = os.listdir(logo_dir)
    print(f"[INFO] logo 目录中有 {len(logo_files)} 个文件: {logo_files[:20]}")

# 启动时写入诊断信息到日志文件
_init_info = {
    "BASE_DIR": BASE_DIR,
    "logo_dir": logo_dir,
    "logo_dir_exists": os.path.exists(logo_dir),
    "logo_files_count": len(os.listdir(logo_dir)) if os.path.exists(logo_dir) else 0,
}
print(f"[INIT] 应用初始化: {json.dumps(_init_info, ensure_ascii=False)}")
# 同时检查 output/logo 目录
output_logo = os.path.join(BASE_DIR, "output", "logo")
if os.path.exists(output_logo):
    print(f"[INIT] output/logo 目录存在，包含 {len(os.listdir(output_logo))} 个文件")
print(f"[INIT] 完整环境变量: PATH={os.environ.get('PATH', 'N/A')}")

# 对外提供 output 目录文件的路由：访问 /output/<文件名> 即下载 output/ 下同名文件
# 注意：EPG(epg.xml.gz) 与 Web 后台共用同一进程/端口，播放器取 EPG 必须带上 /output/ 前缀
@app.route("/output/<path:filename>")
def output_file(filename):
    # send_from_directory 限制只能访问指定目录，防止路径穿越（如 ../）读取系统文件
    return send_from_directory(os.path.join(BASE_DIR, "output"), filename)


# 对外提供 logo 目录文件的路由：访问 /logo/<文件名> 即返回 logo/ 下同名文件
# 频道 logo 图片（如 CCTV1.png）放在 logo/ 目录下，通过 /logo/CCTV1.png 访问
# 日志文件路径（用于容器环境下 print 不可见时的调试）
LOG_DEBUG_FILE = os.path.join(BASE_DIR, "logo_debug.log")

def _log_debug(msg):
    """写入调试日志到文件（容器环境下 print 可能不可见）"""
    try:
        with open(LOG_DEBUG_FILE, "a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}\n")
    except Exception:
        pass

def _find_logo_file(filename):
    """在多个可能目录中搜索 logo 文件，返回绝对路径或 None"""
    # 候选目录列表（按优先级排序）
    candidates = [
        os.path.join(BASE_DIR, "logo"),           # BASE_DIR/logo/
        os.path.join(BASE_DIR, "output", "logo"), # BASE_DIR/output/logo/
        os.path.join(BASE_DIR, "static", "logo"), # BASE_DIR/static/logo/
    ]
    # 也检查 BASE_DIR 的上层目录
    parent = os.path.dirname(BASE_DIR)
    if parent:
        candidates.extend([
            os.path.join(parent, "logo"),
            os.path.join(parent, "output", "logo"),
        ])
    # 再往上两层
    grandparent = os.path.dirname(parent) if parent else None
    if grandparent:
        candidates.extend([
            os.path.join(grandparent, "logo"),
            os.path.join(grandparent, "output", "logo"),
        ])

    _log_debug(f"[SEARCH] 搜索 logo: {filename}, 候选目录: {candidates}")

    for d in candidates:
        if os.path.isdir(d):
            # 精确匹配
            target = os.path.join(d, filename)
            if os.path.isfile(target):
                _log_debug(f"[FOUND] {filename} -> {target}")
                return target
            # 忽略大小写匹配（Linux 文件系统区分大小写）
            for f in os.listdir(d):
                if f.lower() == filename.lower():
                    target = os.path.join(d, f)
                    _log_debug(f"[FOUND] {filename} (case-insensitive) -> {target}")
                    return target
    _log_debug(f"[NOTFOUND] {filename} 在所有候选目录中均未找到")
    return None

@app.route("/logo/<path:filename>")
def logo_file(filename):
    _log_debug(f"[REQUEST] GET /logo/{filename}")
    filepath = _find_logo_file(filename)
    if filepath is not None:
        # send_from_directory 需要传入目录和文件名（不是绝对路径）
        directory = os.path.dirname(filepath)
        actual_name = os.path.basename(filepath)
        _log_debug(f"[SERVE] 返回文件: {filepath}")
        return send_from_directory(directory, actual_name)
    else:
        _log_debug(f"[MISSING] logo 文件不存在: {filename}")
        # 返回一个占位响应（404），同时在日志中标记
        return jsonify({"error": "logo not found", "filename": filename, "searched_dirs": [
            os.path.join(BASE_DIR, "logo"),
            os.path.join(BASE_DIR, "output", "logo"),
            os.path.join(BASE_DIR, "static", "logo"),
        ]}), 404


# 创建 SocketIO 实例，绑定到 app，用于把脚本日志实时推送到浏览器
socketio = SocketIO(app)

# ---------- CORS 跨域安全配置 ----------
# 安全策略：取消 origins="*" 与 supports_credentials=True 的危险组合。
# 允许的跨域来源改由环境变量 CORS_ORIGINS 配置（逗号分隔的具体域名白名单），例如：
#   CORS_ORIGINS="http://localhost:5000,http://192.168.1.10:5000"
# - 配置了白名单：按白名单放行并允许携带凭证（credentials）；
# - 未配置：退化为不放开任何跨域来源（仅同源可用），最安全。
_cors_origins_env = os.environ.get("CORS_ORIGINS", "").strip()
if _cors_origins_env:
    ALLOWED_ORIGINS = [o.strip() for o in _cors_origins_env.split(",") if o.strip()]
    _supports_credentials = True                       # 有具体白名单才允许携带凭证
else:
    ALLOWED_ORIGINS = []                               # 空 = 不放开任何跨域来源
    _supports_credentials = False

CORS(
    app,
    resources={
        r"/*": {                                       # 对所有路径生效
            "origins": ALLOWED_ORIGINS,                # 具体来源白名单，绝不用 * 通配
            "methods": ["GET", "POST", "PUT", "DELETE", "OPTIONS"],
            "allow_headers": ["Content-Type", "Authorization"],
            "expose_headers": ["Content-Type"],
            "supports_credentials": _supports_credentials,  # 仅在白名单非空时开启
            "max_age": 600,                            # 预检(OPTIONS)结果缓存 600 秒
        }
    },
)

# 配置文件绝对路径：<工程根>/config/config.json
CONFIG_PATH = os.path.join(BASE_DIR, "config", "config.json")


def parse_channels():
    """解析 output/sctv.txt，返回频道字典列表。
    每行格式约定：频道名称,rtsp地址#组播地址（# 会被当成分隔符之一）。"""
    channels = []                                   # 收集解析出的频道
    try:
        # 以 UTF-8 打开频道源文件（不存在时走 except 返回空列表）
        with open(os.path.join(BASE_DIR, "output", "sctv.txt"), "r", encoding="utf-8") as f:
            for line in f:                          # 逐行读取
                # 跳过空行、以 # 开头的注释行、以及含 #genre# 的分类标题行
                if line.strip() and not line.startswith("#") and "#genre#" not in line:
                    # 把 # 统一替换为 , 再按逗号切分，得到 [名称, 地址1, 地址2, ...]
                    parts = line.strip().replace("#", ",").split(",")
                    if len(parts) >= 3:             # 至少要有 名称+两个地址 才算有效行
                        name = parts[0].strip()     # 第 1 段是频道名称
                        # 第 2 段：含 "rtsp" 就当单播地址，否则回退取第 3 段
                        rtsp = (
                            parts[1].strip()
                            if "rtsp" in parts[1].strip()
                            else parts[2].strip()
                        )
                        # 第 3 段：不含 "rtsp" 就当组播地址，否则回退取第 2 段
                        multicast = (
                            parts[2].strip()
                            if "rtsp" not in parts[2].strip()
                            else parts[1].strip()
                        )
                        # 组装成字典加入结果列表，键名与模板 index.html 对应
                        channels.append(
                            {"name": name, "rtsp_url": rtsp, "multicast_url": multicast}
                        )
    except FileNotFoundError:
        pass                                        # 文件不存在：返回空列表，交由前端提示
    return channels                                 # 返回全部频道


def config_exists():
    """判断配置文件是否已存在（用于首页决定跳到配置页还是频道页）。"""
    return os.path.exists(CONFIG_PATH)


@app.route("/")
def index():
    """根路径：没有配置就先去配置页，有配置就去频道页。"""
    if not config_exists():
        return redirect("/config")                  # 未配置：重定向到 /config
    return redirect("/channels")                    # 已配置：重定向到 /channels


@app.route("/channels")
def channels():
    """频道列表页：解析频道并渲染 index.html。"""
    channels = parse_channels()                     # 读取解析频道列表
    if not channels:                                # 无数据时带提示文案渲染
        return render_template(
            "index.html", channels=[], message="当前没有频道数据，请点击手动更新"
        )
    return render_template("index.html", channels=channels)  # 正常渲染频道表


@app.route("/config")
def config():
    """配置页面：直接渲染 config.html（读写逻辑在前端调用 /save_config 完成）。"""
    return render_template("config.html")


@app.route("/save_config", methods=["POST"])
def save_config():
    """接收前端提交的 JSON 配置，写入 config.json。"""
    try:
        config_data = request.json                  # 取请求体中的 JSON 数据

        # （原代码此处会强制覆盖 UserAgent，已注释——按需决定是否启用）
        # config_data["UserAgent"] = "Mozilla/5.0 ... SkWebKit-SC-CTC"

        config_dir = os.path.dirname(CONFIG_PATH)   # 取 config.json 所在目录
        if not os.path.exists(config_dir):          # 目录不存在则创建
            os.makedirs(config_dir)

        # 以 UTF-8 写文件：ensure_ascii=False 保留中文，indent=4 便于阅读
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(config_data, f, ensure_ascii=False, indent=4)

        return jsonify({"success": True, "message": "配置保存成功"})  # 成功响应
    except Exception as e:
        # 任何异常都返回 JSON 错误信息，HTTP 状态 500
        return jsonify({"success": False, "message": str(e)}), 500


@app.route("/run_script", methods=["GET"])
def run_script():
    """手动触发更新脚本：校验脚本与解释器存在后，后台异步运行并推日志。"""
    try:
        # 拼接更新脚本绝对路径
        script_path = os.path.join(BASE_DIR, "get_iptv_channels_V1.1.py")
        if not os.path.exists(script_path):         # 脚本不存在 -> 404
            return (
                jsonify({"success": False, "error": f"脚本文件 {script_path} 不存在"}),
                404,
            )

        # 使用当前进程的 Python 解释器（与容器/虚拟环境保持一致）
        python_path = sys.executable
        if not os.path.exists(python_path):         # 解释器路径异常 -> 404
            return (
                jsonify(
                    {"success": False, "error": f"Python解释器 {python_path} 不存在"}
                ),
                404,
            )

        # 用 SocketIO 后台任务运行脚本，避免阻塞当前 HTTP 请求
        socketio.start_background_task(
            target=run_script_with_logging,         # 后台执行函数
            script_path=script_path,                # 传入脚本路径
            python_path=python_path,                # 传入解释器路径
        )
        # 立即返回"已开始"，真正进度靠 SocketIO 事件实时推送
        return jsonify({"success": True, "message": "脚本开始运行"})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


def run_script_with_logging(script_path, python_path):
    """运行脚本并把 stdout/stderr 实时推送给浏览器。"""
    try:
        time.sleep(2)                               # 启动前短暂等待（给前端连上 WS 留时间）
        # 以子进程方式启动脚本：捕获标准输出/错误，按 UTF-8 文本读取
        process = subprocess.Popen(
            [python_path, script_path],             # 命令行：解释器 + 脚本
            stdout=subprocess.PIPE,                 # 接管标准输出管道
            stderr=subprocess.PIPE,                 # 接管标准错误管道
            text=True,                              # 以文本模式读取（非字节）
            encoding="utf-8",                       # 指定解码编码
            universal_newlines=True,                # 统一换行处理
            # 继承当前环境变量并强制关闭 Python 输出缓冲，保证日志实时
            env={**os.environ, "PYTHONUNBUFFERED": "1"},
        )

        def read_stream(stream, stream_type):
            """持续按行读取一个流，每读到一行就 emit 一条 log 事件。"""
            while True:
                output = stream.readline()          # 阻塞读一行
                if output:                          # 读到内容
                    socketio.emit("log", {"data": output.strip(), "type": stream_type})
                else:                               # 读到 EOF（空串）则结束循环
                    break

        # 分别用两个线程读 stdout / stderr，避免一个流阻塞另一个
        stdout_thread = threading.Thread(
            target=read_stream, args=(process.stdout, "stdout")
        )
        stderr_thread = threading.Thread(
            target=read_stream, args=(process.stderr, "stderr")
        )

        stdout_thread.start()                       # 启动读 stdout 线程
        stderr_thread.start()                       # 启动读 stderr 线程

        stdout_thread.join()                        # 等 stdout 读完
        stderr_thread.join()                        # 等 stderr 读完

        # 关键修复：join 线程后子进程可能尚未回收，必须先 wait() 拿到退出码，
        # 否则 process.returncode 仍为 None，会被误判为"成功"。
        process.wait()

        if process.returncode == 0:                 # 退出码 0 视为成功
            socketio.emit("script_complete")        # 推送"脚本完成"事件
        else:                                       # 非 0 视为失败
            socketio.emit(
                "script_failed", {"error": f"脚本退出代码: {process.returncode}"}
            )

        return process.returncode == 0              # 返回布尔结果
    except Exception as e:
        # 运行过程本身异常：推送失败事件并返回 False
        socketio.emit("script_failed", {"error": str(e)})
        return False


@socketio.on("connect")
def handle_connect():
    """WebSocket 连接建立回调：给客户端回一条连接成功提示。"""
    emit("log", {"data": "WebSocket连接成功"})


def setup_cron_job(cron_expression):
    """把一个 cron 表达式写入系统 crontab，使其定时运行更新脚本。
    返回 (是否成功, 说明信息)。"""
    try:
        project_path = BASE_DIR                     # 脚本运行的工作目录（工程根）
        script_path = os.path.join(BASE_DIR, "get_iptv_channels_V1.1.py")  # 脚本绝对路径
        python_path = sys.executable                # 解释器路径

        fields = cron_expression.split()            # 按空格拆 cron 字段
        if len(fields) != 5:                        # 标准 crontab 必须是 5 个字段
            return False, "cron表达式必须包含5个字段"

        def is_valid_cron_field(field):
            """校验单个字段是否只含合法字符（数字与 * / - , ）。"""
            for char in field:
                if not (char.isdigit() or char in ["*", "/", "-", ","]):
                    return False                    # 出现非法字符 -> 不合法
            return True                             # 全部合法

        if not all(is_valid_cron_field(field) for field in fields):
            return False, "无效的cron表达式格式"     # 任一根字段非法即拒绝

        # 组装要写入的 cron 行：先 cd 到工程目录再执行脚本（保证脚本相对路径正确）
        cron_command = (
            f"{cron_expression} cd {project_path} && {python_path} {script_path}"
        )

        # 读取现有 crontab（crontab -l 列出当前用户所有定时任务）
        process = subprocess.run(
            ["crontab", "-l"], stdout=subprocess.PIPE, stderr=subprocess.PIPE
        )

        stderr_output = process.stderr.decode("utf-8")
        if process.returncode != 0:                 # 列出不成功，需要区分原因
            if "no crontab for" in stderr_output:   # 该用户还没有 crontab
                cron_content = ""                   # 视为空内容
            elif "permission denied" in stderr_output.lower():
                return False, "没有权限访问crontab，请使用sudo运行"
            else:                                   # 其它未知错误
                return False, f"获取crontab失败: {stderr_output}"
        else:
            cron_content = process.stdout.decode("utf-8")  # 正常拿到现有内容

        if cron_command in cron_content:            # 已存在完全相同的任务则不重复添加
            return True, "定时任务已存在"

        new_cron = cron_content + "\n" + cron_command + "\n"  # 追加新任务
        # 把新的完整 crontab 写回系统（输入通过 stdin 传入）
        process = subprocess.run(
            ["crontab", "-"],
            input=new_cron.encode("utf-8"),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

        if process.returncode != 0:                 # 写入失败
            return False, f"写入crontab失败: {process.stderr.decode('utf-8')}"

        return True, "定时任务添加成功"             # 成功
    except Exception as e:
        # 兜底异常：带上完整堆栈便于排查
        return False, f"设置定时任务时发生错误: {str(e)}\n{traceback.format_exc()}"


@app.route("/set_cron", methods=["POST"])
def set_cron():
    """接口：接收 cron 表达式并调用 setup_cron_job 写入定时任务。"""
    try:
        # （以下为排查跨域/请求体问题的调试打印，生产环境建议移除或改 logging）
        # print("Request headers:", request.headers)
        # print("Content-Type:", request.headers.get("Content-Type"))

        if not request.is_json:                     # 请求头不是 application/json
            return (
                jsonify({"success": False, "error": "请求头必须为application/json"}),
                400,
            )

        data = request.get_json()                   # 解析 JSON 请求体

        if not data:                                # 空请求体
            return jsonify({"success": False, "error": "未接收到有效数据"}), 400

        cron_expression = data.get("cron_expression")  # 取出 cron 表达式字段

        if not cron_expression:                     # 字段缺失/为空
            return jsonify({"success": False, "error": "请输入有效的cron表达式"}), 400

        success, message = setup_cron_job(cron_expression)  # 执行写入
        return jsonify({"success": success, "message": message})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/get_cron", methods=["GET"])
def get_cron():
    """接口：查询当前与本脚本相关的定时任务行。"""
    try:
        process = subprocess.run(                   # 列出 crontab
            ["crontab", "-l"], stdout=subprocess.PIPE, stderr=subprocess.PIPE
        )
        if process.returncode != 0:                 # 没有 crontab
            return jsonify(
                {
                    "success": True,
                    "cron_expression": None,
                    "message": "没有设置定时任务",
                }
            )

        cron_content = process.stdout.decode("utf-8")
        script_path = os.path.join(BASE_DIR, "get_iptv_channels_V1.1.py")
        for line in cron_content.splitlines():      # 逐行找包含本脚本的任务行
            if script_path in line:
                # 直接返回整行（含表达式与命令），前端如需只要表达式可再截断
                return jsonify({"success": True, "cron_expression": line})

        # 遍历完也没找到相关任务
        return jsonify(
            {"success": True, "cron_expression": None, "message": "没有找到定时任务"}
        )
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/delete_cron", methods=["POST", "DELETE"])
def delete_cron():
    """接口：只删除与本脚本相关的定时任务行，不再清空整个 crontab。

    原实现用 `crontab -r` 会一次性清空当前用户的全部定时任务，属于误删风险。
    这里改为：读取现有 crontab -> 过滤掉包含本脚本路径的行 -> 把剩余内容写回；
    若过滤后已无任何任务，才移除 crontab。这样其它无关任务不受影响。
    """
    try:
        script_path = os.path.join(BASE_DIR, "get_iptv_channels_V1.1.py")

        # 1) 读取现有 crontab
        process = subprocess.run(
            ["crontab", "-l"], stdout=subprocess.PIPE, stderr=subprocess.PIPE
        )
        stderr_output = process.stderr.decode("utf-8")

        if process.returncode != 0:
            if "no crontab for" in stderr_output:
                # 本来就没有 crontab，无需删除
                return jsonify({"success": True, "message": "当前没有定时任务"})
            if "permission denied" in stderr_output.lower():
                return (
                    jsonify({"success": False, "error": "没有权限访问crontab，请使用sudo运行"}),
                    403,
                )
            return jsonify({"success": False, "error": stderr_output}), 500

        cron_content = process.stdout.decode("utf-8")

        # 2) 过滤：保留不包含本脚本路径的行，并统计删除条数
        kept_lines = []
        removed = 0
        for line in cron_content.splitlines():
            if script_path in line:
                removed += 1
            else:
                kept_lines.append(line)

        if removed == 0:
            return jsonify({"success": True, "message": "没有找到与本脚本相关的定时任务"})

        # 3) 写回剩余内容；若过滤后已无任务则移除 crontab
        remaining = "\n".join(kept_lines).strip()
        if remaining:
            write = subprocess.run(
                ["crontab", "-"],
                input=(remaining + "\n").encode("utf-8"),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            if write.returncode != 0:
                return (
                    jsonify({"success": False, "error": write.stderr.decode("utf-8")}),
                    500,
                )
        else:
            subprocess.run(
                ["crontab", "-r"], stdout=subprocess.PIPE, stderr=subprocess.PIPE
            )

        return jsonify({"success": True, "message": f"已删除 {removed} 条与本脚本相关的定时任务"})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500

@app.route("/logo_debug")
def logo_debug():
    """调试路由：列出 logo 目录中的所有文件，帮助排查 404 问题。"""
    try:
        ld = os.path.join(BASE_DIR, "logo")
        if os.path.exists(ld):
            files = os.listdir(ld)
            return jsonify({"success": True, "logo_dir": ld, "files": files})
        else:
            return jsonify({"success": False, "logo_dir": ld, "error": "logo directory does not exist"})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)})

@app.route("/check_logo")
def check_logo():
    """诊断接口：检查指定 logo 文件是否可找到，返回详细诊断信息。"""
    fname = request.args.get("file", "CCTV1.png")
    result = {
        "requested_file": fname,
        "base_dir": BASE_DIR,
        "logo_dir": os.path.join(BASE_DIR, "logo"),
        "logo_dir_exists": os.path.exists(os.path.join(BASE_DIR, "logo")),
        "output_logo_dir": os.path.join(BASE_DIR, "output", "logo"),
        "output_logo_dir_exists": os.path.exists(os.path.join(BASE_DIR, "output", "logo")),
        "static_logo_dir": os.path.join(BASE_DIR, "static", "logo"),
        "static_logo_dir_exists": os.path.exists(os.path.join(BASE_DIR, "static", "logo")),
    }
    # 列出各目录内容
    for key, dirpath in [
        ("logo_dir_files", os.path.join(BASE_DIR, "logo")),
        ("output_logo_dir_files", os.path.join(BASE_DIR, "output", "logo")),
        ("static_logo_dir_files", os.path.join(BASE_DIR, "static", "logo")),
    ]:
        if os.path.isdir(dirpath):
            result[key] = os.listdir(dirpath)
        else:
            result[key] = "目录不存在"
    # 检查请求的文件是否被找到
    found = _find_logo_file(fname)
    result["file_found"] = found is not None
    result["file_absolute_path"] = found
    return jsonify(result)

@app.route("/routes_debug")
def routes_debug():
    """调试路由：列出所有注册的路由。"""
    routes = []
    for rule in app.url_map.iter_rules():
        routes.append({"rule": str(rule), "methods": list(rule.methods), "endpoint": rule.endpoint})
    return jsonify({"routes": routes})

if __name__ == "__main__":
    # 仅当作为主程序直接运行时才启动服务：
    # host=0.0.0.0 监听所有网卡；port=3000 端口（改端口需同步改 Dockerfile EXPOSE 与 -p 映射）
    # debug=True 开启调试（生产环境应关闭）；allow_unsafe_werkzeug 允许用 Werkzeug 跑生产(不推荐)
    socketio.run(app, host="0.0.0.0", port=3000, debug=True, allow_unsafe_werkzeug=True)
