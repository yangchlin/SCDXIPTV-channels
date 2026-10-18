#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
IPTV频道获取与EPG同步工具 V1.1
功能: 从IPTV服务器获取频道列表、生成m3u/txt播放列表、获取EPG节目指南并同步到Git仓库
"""

# ==================== 标准库导入 ====================
import re               # 正则表达式模块，用于解析HTML和文本中的数据
import random           # 随机数模块，用于从多个key中随机选择一个
import time             # 时间模块，用于延时重试
import json             # JSON序列化/反序列化模块
import os               # 操作系统接口模块，用于文件路径操作
from datetime import datetime  # 日期时间模块，用于时间戳转换
from pathlib import Path     #  pathlib路径操作模块，用于路径判断
from typing import Dict, List, Tuple, Optional, Callable  # 类型提示模块
import logging          # 日志记录模块
from logging.handlers import RotatingFileHandler  # 轮转日志处理器，自动管理日志文件大小
import subprocess       # 子进程模块，用于执行git命令
import shutil           # 高级文件操作模块，用于目录复制/删除
import gzip             # Gzip压缩模块，用于生成.xml.gz压缩文件
from dataclasses import dataclass  # dataclass装饰器，用于自动生成__init__等魔术方法
from urllib.parse import urlparse  # URL解析模块，用于从URL中提取域名

# ==================== 第三方库导入 ====================
import requests         # HTTP请求库，用于发送网络请求
from bs4 import BeautifulSoup  # HTML解析库（保留供后续使用）
from crypt_1 import PRPCrypt, find_key  # 自定义加密模块：PRPCrypt用于加密，find_key用于解密Authenticator

# ==================== 日志配置 ====================
# 创建当前模块的日志记录器
logger = logging.getLogger(__name__)
# 设置日志级别为INFO，低于INFO级别的日志（如DEBUG）不会被记录
logger.setLevel(logging.INFO)

# ==================== 路径常量定义 ====================
# 配置文件路径：脚本同目录下的config/config.json
CONFIG_PATH = os.path.join(os.path.dirname(__file__), "config", "config.json")

# ==================== 日志格式器 ====================
# 创建日志输出格式：时间 - 日志器名称 - 日志级别 - 消息内容
formatter = logging.Formatter(
    '%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'  # 日期时间格式：年-月-日 时:分:秒
)

# ==================== 文件日志处理器 ====================
# 创建轮转文件处理器：日志文件最大4MB，保留3个备份文件，UTF-8编码
file_handler = RotatingFileHandler(
    os.getcwd() + '/log/iptv.log',  # 日志文件路径（当前工作目录下的log/iptv.log）
    maxBytes=4 * 1024 * 1024,      # 单个日志文件最大4MB（4*1024*1024字节）
    backupCount=3,                  # 保留3个备份日志文件
    encoding='utf-8'                # 文件编码为UTF-8
)
file_handler.setFormatter(formatter)  # 设置文件日志的输出格式

# ==================== 控制台日志处理器 ====================
# 创建控制台输出处理器（stderr）
console_handler = logging.StreamHandler()
console_handler.setFormatter(formatter)  # 设置控制台日志的输出格式

# ==================== 注册日志处理器 ====================
logger.addHandler(file_handler)   # 将文件处理器添加到日志器
logger.addHandler(console_handler)  # 将控制台处理器添加到日志器

# ==================== 配置数据类 ====================
@dataclass
class Config:
    """
    配置管理类（数据类）
    使用dataclass装饰器自动生成__init__、__repr__等魔术方法
    """
    key: str = ''                  # 加密密钥
    UserID: str = ''               # 用户ID
    mac: str = ''                  # 机顶盒MAC地址
    STBID: str = ''                # 机顶盒ID
    ip: str = ''                   # IP地址
    STBType: str = ''              # 机顶盒类型
    STBVersion: str = ''           # 机顶盒版本号
    UserAgent: str = ''            # HTTP请求User-Agent字符串
    Authenticator: str = ''        # 认证令牌（加密后）
    RTP2HTTPD: str = ''            # RTP转HTTP代理服务地址
    templateName: str = ''         # 模板名称
    areaId: str = ''               # 区域ID
    epg_git_repo: str = ''         # EPG Git仓库URL
    epg_git_private_token: str = '' # Git私有令牌
    epg_server_url: str = ''       # EPG服务器地址配置（容器IP:端口），用于M3U头中的x-tvg-url
    epgname: str = ''              # EPG输出文件名（gzip压缩格式）
    logo_url: str = ''             # 频道logo基础地址(可填自定义地址,留空则自动用EPG地址);脚本会自动拼 /logo/{频道名}.png
    merge_output_files: bool = True  # 是否把 output/iptv.txt 里的频道合并进最终播放列表

    @classmethod
    def load(cls, config_file: str = CONFIG_PATH) -> 'Config':
        """
        类方法：从JSON配置文件加载配置
        :param config_file: 配置文件路径，默认为CONFIG_PATH
        :return: 配置对象实例
        """
        config = cls()  # 创建Config实例（所有字段为空字符串）
        # 检查配置文件是否存在
        if not Path(config_file).exists():
            logger.warning(f"配置文件 {config_file} 不存在")
            return config  # 文件不存在时返回空配置对象

        try:
            # 以UTF-8编码读取JSON配置文件
            with open(config_file, 'r', encoding='utf-8') as f:
                data = json.load(f)  # 解析JSON文件内容为字典
                # 遍历Config类的所有注解字段
                for field in cls.__annotations__:
                    # 如果JSON数据中包含该字段，则设置到配置对象中
                    if field in data:
                        setattr(config, field, data[field])
        except Exception as e:
            # 捕获所有异常并记录错误日志
            logger.error(f"加载配置文件失败: {str(e)}")
        return config

    def save(self, config_file: str = CONFIG_PATH) -> bool:
        """
        实例方法：将配置对象保存为JSON文件
        :param config_file: 配置文件路径，默认为CONFIG_PATH
        :return: 保存成功返回True，失败返回False
        """
        try:
            # 以UTF-8编码写入JSON文件（带缩进格式化）
            with open(config_file, 'w', encoding='utf-8') as f:
                # 构建字段名->字段值的字典
                data = {field: getattr(self, field) for field in self.__annotations__}
                # 序列化并写入文件（ensure_ascii=False保证中文不被转义）
                json.dump(data, f, indent=4, ensure_ascii=False)
            return True  # 保存成功
        except Exception as e:
            # 捕获异常并记录错误日志
            logger.error(f"保存配置文件失败: {str(e)}")
            return False  # 保存失败


# ==================== 配置管理函数 ====================
def load_config(config_file=CONFIG_PATH):
    """
    加载配置文件（返回字典格式）
    :param config_file: 配置文件路径
    :return: 配置字典，加载失败返回空字典
    """
    config = {}  # 初始化空配置字典
    # 检查配置文件是否存在
    if os.path.exists(config_file):
        try:
            # 以UTF-8编码读取并解析JSON配置文件
            with open(config_file, 'r', encoding='utf-8') as f:
                config = json.load(f)
        except json.JSONDecodeError as e:
            # JSON格式解析错误
            logger.error(f'配置文件格式错误: {str(e)}')
        except Exception as e:
            # 其他未知错误
            logger.error(f'加载配置文件失败: {str(e)}')
    return config  # 返回配置字典（可能为空）


def update_config(key=None, value=None, config_file=CONFIG_PATH):
    """
    更新配置文件中单个配置项
    :param key: 配置项键名
    :param value: 配置项值
    :param config_file: 配置文件路径
    :return: 更新成功返回True，失败返回False
    """
    # 参数校验：key和value都必须非空
    if not key or not value:
        logger.error('更新配置需要提供key和value')
        return False

    try:
        # 读取现有配置
        config = load_config(config_file)
        # 更新指定配置项
        config[key] = value
        # 将更新后的配置写回文件（UTF-8编码，带缩进）
        with open(config_file, 'w', encoding='utf-8') as f:
            json.dump(config, f, indent=4, ensure_ascii=False)
        logger.info(f'成功更新配置项 {key} = {value}')
        return True  # 更新成功
    except Exception as e:
        # 捕获异常并记录错误日志
        logger.error(f'更新配置文件失败: {str(e)}')
        return False  # 更新失败


# ==================== 全局配置加载 ====================
# 加载配置文件为字典
config = load_config()

# 从配置文件获取key（优先级：config.json中的key > 从Authenticator解密获取）
key = config.get('key', '')
# 如果key为空且存在Authenticator，则尝试解密获取
if not key and config.get('Authenticator', ''):
    try:
        # 调用find_key函数解密Authenticator，返回可能的key列表
        keys = find_key(config['Authenticator'])
        if keys:
            # 从解密得到的key列表中随机选择一个（增加安全性）
            key = random.choice(keys)
            logger.info(f"从Authenticator解密结果中随机选择key: {key}")
            # 将获取到的key更新回配置文件
            update_config('key', key)
    except Exception as e:
        logger.error(f"解密Authenticator时发生错误: {str(e)}")

# 从配置文件加载各配置项（使用get方法设置默认值为空字符串）
UserID = config.get('UserID', '')                            # 用户ID
mac = config.get('mac', '')                                  # 机顶盒MAC地址
STBID = config.get('STBID', '')                              # 机顶盒ID
ip = config.get('ip', '')                                    # IP地址
STBType = config.get('STBType', '')                          # 机顶盒类型
STBVersion = config.get('STBVersion', '')                    # 机顶盒版本号
UserAgent = config.get('UserAgent', '')                      # User-Agent字符串
Authenticator = config.get('Authenticator', '')              # 认证令牌
X_Requested_With = "com.huawei.iptv"                         # 华为IPTV请求头标识
templateName = config.get('templateName', '')                # 模板名称
areaId = config.get('areaId', '')                            # 区域ID
epg_server_url = config.get('epg_server_url') or ''          # EPG服务器地址，用于M3U头中的x-tvg-url
epgname = config.get('epgname') or 'epg.xml.gz'              # EPG输出文件名（gzip压缩格式）
logo_url = config.get('logo_url') or ''                      # 频道logo地址；(留空)则使用 EPG 地址
merge_output_files = config.get('merge_output_files', True)  # 默认开启 output/iptv.txt 合并


# ==================== 必要配置项校验 ====================
# 定义必须配置的配置项及其对应变量
required_configs = {
    'UserID': UserID,
    'mac': mac,
    'STBID': STBID,
    'Authenticator': Authenticator
}
# 遍历检查必要配置项是否为空
for name, value in required_configs.items():
    if not value:
        logger.error(f"缺少必要配置项: {name}")
        raise ValueError(f"请在config.json中配置{name}")  # 缺少必要配置时抛出异常终止程序


# ==================== 认证函数 ====================
def get_auth(max_retries: int = 3) -> Optional[Tuple[str, dict, str, str]]:
    """
    获取认证信息（四步认证流程）
    第一步：获取服务器host地址
    第二步：获取EncryptToken和userToken
    第三步：使用PRPCrypt加密生成Authenticator
    第四步：发送认证请求验证身份
    :param max_retries: 最大重试次数，默认3次
    :return: 成功返回 (host, cookies, user_token, stbid) 元组，失败返回None
    """
    # 获取认证服务器地址（配置文件中的AuthenticationIP，有默认值）
    AuthenticationIP = config.get('AuthenticationIP', 'http://182.138.3.142:8082')
    retry_count = 0  # 初始化重试计数器

    # 重试循环：最多重试max_retries次
    while retry_count < max_retries:
        try:
            # ===== 第一步：获取host =====
            # 构造认证URL，携带UserID和Action参数
            url = f'{AuthenticationIP}/EDS/jsp/AuthenticationURL?UserID={UserID}&Action=Login'
            # 构造HTTP请求头
            headers = {
                'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',  # 可接受的内容类型
                'User-Agent': UserAgent,           # 浏览器标识
                'X-Requested-With': X_Requested_With,  # 华为IPTV标识
            }
            # 记录尝试日志
            logger.info(f'尝试获取认证信息 (尝试 {retry_count + 1}/{max_retries})')
            # 发送GET请求获取认证URL响应（超时10秒）
            response = requests.get(url, headers=headers, timeout=10)
            response.raise_for_status()  # 如果HTTP状态码表示错误则抛出异常

            # 将响应内容保存到日志文件供调试
            with open(os.getcwd() + "/log/AuthenticationURL.jsp", "w", encoding="utf-8") as f:
                f.write(response.text)
            logger.debug(f"请求返回内容：\n{response.text}")

            # 从响应URL中解析出主机地址（netloc部分）
            host = urlparse(response.url).netloc
            logger.info(f"解析得到服务器地址: {host}")

            # ===== 第二步：获取token =====
            # 构造登录认证URL
            auth_url = f'http://{host}/EPG/jsp/authLoginHWCTC.jsp'
            # 构造登录请求头
            auth_headers = {
                'User-Agent': UserAgent,
                'Content-Type': 'application/x-www-form-urlencoded',  # 表单提交格式
                'Referer': f'http://{host}/EPG/jsp/AuthenticationURL?UserID={UserID}&Action=Login',  # 引用页
                'X-Requested-With': X_Requested_With,
            }
            # 构造登录请求数据
            auth_data = {
                'UserID': UserID,
                'VIP': ''
            }
            # 发送POST请求进行登录认证（超时10秒）
            auth_response = requests.post(auth_url, headers=auth_headers, data=auth_data, timeout=10)
            auth_response.raise_for_status()  # 检查响应状态

            # 记录登录响应内容到日志文件
            logger.debug(f"authLogin响应内容：\n{auth_response.text}")
            with open(os.getcwd() + "/log/authLoginHWCTC.jsp", "w", encoding="utf-8") as f:
                f.write(auth_response.text)

            # 解析HTML内容（使用BeautifulSoup，当前未使用但保留供扩展）
            soup = BeautifulSoup(auth_response.text, 'html.parser')

            # 使用正则表达式匹配EncryptToken（JavaScript变量赋值）
            encrypt_token_pattern = r'var EncryptToken = "([^"]+)"'
            encrypt_match = re.search(encrypt_token_pattern, auth_response.text)
            if not encrypt_match:
                raise ValueError('无法找到EncryptToken')  # 未找到则抛出异常
            EncryptToken = encrypt_match.group(1)  # 提取EncryptToken值

            # 使用正则表达式匹配userToken（JavaScript赋值语句）
            user_token_pattern = r'document\.authform\.userToken\.value = "([^"]+)"'
            user_token_match = re.search(user_token_pattern, auth_response.text)
            if not user_token_match:
                raise ValueError('无法找到userToken')  # 未找到则抛出异常
            userToken = user_token_match.group(1)  # 提取userToken值

            # ===== 第三步：生成Authenticator =====
            # 使用PRPCrypt加密类，传入key初始化加密器
            pc = PRPCrypt(key)
            # 构造待加密字符串（格式：key$EncryptToken$UserID$STBID$ip$mac$$CTC）
            auth_str = f'{key}${EncryptToken}${UserID}${STBID}${ip}${mac}$$CTC'
            logger.info(f"加密前字符串: {auth_str}")
            # 执行加密操作
            Authenticator = pc.encrypt(auth_str)
            logger.info(f"加密后Authenticator: {Authenticator}")

            # ===== 第四步：验证认证 =====
            # 构造认证验证URL
            valid_url = f'http://{host}/EPG/jsp/ValidAuthenticationHWCTC.jsp'
            # 构造验证请求头
            valid_headers = {
                'User-Agent': UserAgent,
                'Content-Type': 'application/x-www-form-urlencoded',
                'Referer': f'http://{host}/EPG/jsp/authLoginHWCTC.jsp',
                'X-Requested-With': X_Requested_With,
            }
            # 构造验证请求数据（包含所有认证参数）
            valid_data = {
                'UserID': UserID,
                'Lang': '0',
                'SupportHD': '1',
                'NetUserID': '',
                'Authenticator': Authenticator,
                'STBType': STBType,
                'STBVersion': STBVersion,
                'conntype': 'dhcp',
                'STBID': STBID,
                'templateName': templateName,
                'areaId': areaId,
                'userToken': userToken,
                'userGroupId': '1',
                'productPackageId': '',
                'mac': mac,
                'UserField': '',
                'SoftwareVersion': '',
                'IsSmartStb': '',
                'desktopId': '',
                'stbmaker': '',
                'XMPPCapability': '',
                'ChipID': '',
                'VIP': '',
            }
            # 发送POST请求验证认证（超时10秒）
            valid_response = requests.post(valid_url, headers=valid_headers, data=valid_data, timeout=10)
            valid_response.raise_for_status()

            # 记录验证响应内容
            logger.debug(f"验证认证响应内容：\n{valid_response.text}")

            # 获取服务器返回的cookies（包含JSESSIONID等会话信息）
            cookies = valid_response.cookies
            logger.info(f"JSESSIONID: {cookies.get('JSESSIONID')}")

            # 如果响应内容是HTML格式，使用正则表达式提取UserToken和stbid
            if valid_response.headers.get('Content-Type', '').startswith('text/html'):
                html_content = valid_response.text
                # 保存HTML内容到日志文件供调试
                with open(os.getcwd() + "/log/ValidAuthenticationHWCTC.jsp", "w", encoding="utf-8") as f:
                    f.write(html_content)

                # 使用正则表达式提取隐藏字段UserToken
                user_token_match = re.search(r'name="UserToken"\s*value="([^"]+)"', html_content)
                user_token = user_token_match.group(1) if user_token_match else None

                # 使用正则表达式提取隐藏字段stbid
                stbid_match = re.search(r'name="stbid"\s*value="([^"]+)"', html_content)
                stbid = stbid_match.group(1) if stbid_match else None

                # 检查是否成功提取到必要字段
                if not user_token or not stbid:
                    logger.error("无法从HTML中提取UserToken或stbid")
                    return None, None, None, None  # 提取失败返回None

                logger.info(f"从HTML中提取到UserToken: {user_token}")
                logger.info(f"从HTML中提取到stbid: {stbid}")
                # 认证成功，返回host、cookies、user_token、stbid
                return host, cookies, user_token, stbid

        except requests.exceptions.RequestException as e:
            # 捕获所有网络请求异常（连接超时、DNS失败、HTTP错误等）
            logger.error(f'请求失败: {str(e)}')
            retry_count += 1  # 重试计数+1
            if retry_count < max_retries:
                logger.info(f'等待5秒后重试...')
                time.sleep(5)  # 等待5秒后继续下一次重试
            continue  # 继续下一次循环重试


# ==================== 时间格式转换函数 ====================
def format_xmltv_time(timestamp):
    """
    将时间戳字符串转换为XMLTV格式时间
    :param timestamp: 原始时间戳字符串，格式为YYYYMMDDHHmmSS
    :return: XMLTV格式时间字符串（纯时间部分，不含时区）
    """
    # 将字符串解析为datetime对象（格式：年月日时分秒）
    dt = datetime.strptime(timestamp, '%Y%m%d%H%M%S')
    # 格式化为XMLTV要求的时间字符串
    return dt.strftime('%Y%m%d%H%M%S')



# ==================== 频道名称清理函数 ====================
def clean_channel_name(name: str) -> str:
    """
    清理频道名称：去掉末尾的'高清'、'HD'、'FHD'、'UHD'、'4K'等后缀，去掉'-'连字符，并去除首尾空格
    :param name: 原始频道名称
    :return: 清理后的频道名称
    """
    name = name.strip()  # 去除首尾空格
    # 依次检查并去掉各种高清后缀
    for suffix in ["高清", "HD", "FHD", "UHD"]:
        if name.endswith(suffix):
            name = name[:-len(suffix)].rstrip()  # 去掉后缀并去除右侧空格
    # 去掉频道名称中的'-'连字符（如 CCTV-1 -> CCTV1, CCTV-少儿 -> CCTV少儿）
    name = name.replace('-', '')
    return name


# ==================== 频道数据处理函数 ====================
def read_extra_channels_from_output() -> "List[Tuple[str, str]]":
    """
    读取 output 目录下 iptv.txt 里的额外频道，返回 [(name, url), ...]
    - iptv.txt 格式：每行 频道名,播放地址[,分组]（逗号分隔，分组可省略）
    读取失败或文件不存在时安全跳过，返回已解析到的部分。
    """
    extras = []
    out_dir = os.getcwd() + '/output'

    # 解析 iptv.txt：name,url[,group]
    txt_path = os.path.join(out_dir, 'iptv.txt')
    if os.path.exists(txt_path):
        try:
            with open(txt_path, 'r', encoding='utf-8') as f:
                for raw in f:
                    line = raw.strip()
                    if not line or line.startswith('#'):
                        continue
                    parts = [p.strip() for p in line.split(',')]
                    if len(parts) >= 2 and parts[0] and parts[1]:
                        extras.append((parts[0], parts[1]))
        except Exception as e:
            print('[WARN] 解析 iptv.txt 失败: %s' % e)


    # 去重：按(频道名, URL)去重，避免iptv.txt内部重复
    seen = set()
    deduped = []
    for name, url in extras:
        key = (name.strip(), url.strip())
        if key not in seen:
            seen.add(key)
            deduped.append((name, url))
    return deduped



def process_channel_data(channels: List[Tuple[str, ...]]) -> Dict[str, List[str]]:
    """
    处理频道数据并生成频道信息映射，同时写入sctv.txt和sctv.m3u文件
    :param channels: 频道数据列表，每个元素为元组(channel_id, name, user_channel_id, channel_url, timeshift, timeshift_length, rtsp_url, fcc_enable, fcc_ip, fcc_port, fec_port)
    :return: 频道信息字典 {channel_id: [name, user_channel_id]}
    """
    channel_info = {}  # 初始化频道信息字典
    channel_ids = []   # 初始化频道ID列表（用于后续映射）
    used_urls = set()  # 初始化已使用URL集合（用于去重）
    used_names = set()  # 初始化已使用频道名集合（保留供外部合并使用）
    used_mc_urls = set()  # 已使用组播地址集合（用于外部rtp地址去重）

    # 过滤掉包含"画中画"和"单音轨"的无效频道
    channels = [ch for ch in channels if "画中画" not in ch[1] and "单音轨" not in ch[1]]

    # 使用with语句同时打开两个输出文件（自动管理文件关闭）
    with open(os.getcwd() + '/output/sctv.txt', 'w', encoding='utf-8') as ftxt, \
         open(os.getcwd() + '/output/sctv.m3u', 'w', encoding='utf-8') as fm3u:

        # 写入M3U文件头（含name和x-tvg-url属性，指向EPG服务器地址）
        epg_url = f'{epg_server_url}/output/{epgname}'
        fm3u.write('#EXTM3U name="四川电信(Huawei)" x-tvg-url="{}"\n'.format(epg_url))

        # 按原始顺序写入所有频道（不再按分类排序）
        for channel in channels:
            # 清理频道名称：去掉"高清"等后缀，特殊处理CCTV-少儿
            name = clean_channel_name("CCTV-14" if channel[1] == "CCTV-少儿" else channel[1])
            #name = clean_channel_name(channel[1])
            # 不去重：保留服务器返回的所有频道（包括同名不同地址的）
            # 获取RTP2HTTPD代理服务地址（配置文件中有则用配置，否则用默认值）
            RTP2HTTPD = config.get('RTP2HTTPD', '')
            # 构造基础RTP播放URL
            url = f'{RTP2HTTPD}/rtp/{channel[3]}'
            # 提取组播地址(239.x.x.x:port)用于外部rtp频道去重
            _mc = re.match(r'(239\.\d+\.\d+\.\d+:\d+)', channel[3])
            if _mc:
                used_mc_urls.add(_mc.group(1))
            append = False  # 标记是否已添加查询参数

            # 如果FCCEnable字段值为'2'，表示启用FCC（频率转换）
            if channel[7] == '2':
                # 在URL后追加fcc参数（格式：IP:端口）
                url = f'{url}?fcc={channel[8]}:{channel[9]}'
                append = True  # 标记已添加参数

            # 如果FEC字段存在且不为'0'，表示启用FEC（前向纠错）
            if channel[10] and channel[10] != '0':
                if append:
                    # 如果已有查询参数，用&连接
                    url = f'{url}&fec={channel[10]}'
                else:
                    # 如果没有参数，用?连接
                    url = f'{url}?fec={channel[10]}'

            # 构造时移参数（当TimeShift字段为'1'时启用时移功能）
            TIMESHFIT = f'&r2h-seek-mode=range({channel[5]})' if channel[4] == '1' else ''

            # 构造rtspURL（支持时移的源地址）
            # 将rtsp://替换为RTP2HTTPD的rtsp代理地址，并追加playseek参数
            # 去掉.smil后面的查询参数（如 ?rrsip=...&accountinfo=...）
            rtsp_raw = channel[6]
            if '.smil?' in rtsp_raw:
                rtsp_raw = rtsp_raw.split('.smil?')[0] + '.smil'
            rtspUrl = rtsp_raw.replace(
                "rtsp://", f'{RTP2HTTPD}/rtsp/'
            ) + '?playseek=${(b)yyyyMMddHHmmss}-${(e)yyyyMMddHHmmss}' + TIMESHFIT if channel[4] == '1' else None

            # 根据是否支持时移，写入不同的txt格式
            if rtspUrl is not None:
                # 支持时移：格式为 name,rtspURL#url（#前为时移地址，#后为直播地址）
                ftxt.write(f'{name},{rtspUrl}#{url}\n')
            else:
                # 不支持时移：格式为 name,url
                ftxt.write(f'{name},{url}\n')

            # 构造m3u的catchup属性（用于直播回放功能）
            m3uPlaybackFormat = f'catchup="default" catchup-source="{rtspUrl}"' if rtspUrl is not None else ""
            # 构建tvg属性（logo地址、频道ID、频道名称）
            # logo基础地址：优先用 config 配置的 logo_url；未配置则回退用 EPG 地址(epg_server_url)
            logo_base = (logo_url or epg_server_url).rstrip('/')
            tvg_logo = f'{logo_base}/logo/{name}.png'
            # 构造m3u属性部分（tvg属性 + catchup属性），处理空格
            if m3uPlaybackFormat:
                m3u_attrs = f'tvg-logo="{tvg_logo}" tvg-id="{name}" tvg-name="{name}" {m3uPlaybackFormat}'
            else:
                m3u_attrs = f'tvg-logo="{tvg_logo}" tvg-id="{name}" tvg-name="{name}"'
            # 写入m3u频道条目（EXTINF标记 + URL，含tvg属性不含group-title）
            fm3u.write(f'#EXTINF:-1 {m3u_attrs}, {name}\n{url}\n')
            used_urls.add(url)
            # used_names 已不再用于去重

            # 记录频道信息到列表（channel_id, name, user_channel_id）
            channel_ids.append([channel[0], name, channel[2]])

        # ===== 合并 output 目录里的 iptv.txt 额外频道（按播放地址+频道名+组播地址去重）=====
        if merge_output_files:
            merged_extra = read_extra_channels_from_output()
            logo_base = (logo_url or epg_server_url).rstrip('/')
            RTP2HTTPD = config.get('RTP2HTTPD', '')

            for ex_name, ex_url in merged_extra:
                # 对外部频道名也做同样的清理（去掉'-'连字符等）
                ex_name = clean_channel_name(ex_name)
                # 解析外部URL，统一转换为爬取代码的格式：
                # 爬取代码写入sctv.txt格式: name,http://RTP2HTTPD/rtsp/...#http://RTP2HTTPD/rtp/...
                # 爬取代码写入sctv.m3u格式: http://RTP2HTTPD/rtp/...
                # 外部文件可能使用 rtp:// / rtsp:// / http:// 等多种格式，需统一转换

                rtsp_url = None
                rtp_url = None

                if '#' in ex_url:
                    # 有#分隔：前面是rtsp(时移)地址，后面是rtp(直播)地址
                    parts = ex_url.split('#', 1)
                    raw_rtsp = parts[0].strip()
                    raw_rtp = parts[1].strip()
                    # 转换rtsp部分
                    if raw_rtsp.startswith('rtsp://'):
                        rtsp_url = f'{RTP2HTTPD}/rtsp/{raw_rtsp[len("rtsp://"):]}'
                    elif raw_rtsp.startswith('http://') or raw_rtsp.startswith('https://'):
                        rtsp_url = raw_rtsp
                    else:
                        rtsp_url = raw_rtsp
                    # 转换rtp部分
                    if raw_rtp.startswith('rtp://'):
                        rtp_url = f'{RTP2HTTPD}/rtp/{raw_rtp[len("rtp://"):]}'
                    elif raw_rtp.startswith('http://') or raw_rtp.startswith('https://'):
                        rtp_url = raw_rtp
                    else:
                        rtp_url = raw_rtp
                else:
                    # 没有#，判断协议类型进行转换
                    if ex_url.startswith('rtp://'):
                        # 原始rtp格式 -> http://RTP2HTTPD/rtp/...
                        rtp_url = f'{RTP2HTTPD}/rtp/{ex_url[len("rtp://"):]}'
                    elif ex_url.startswith('rtsp://'):
                        # 原始rtsp格式 -> http://RTP2HTTPD/rtsp/...
                        rtsp_url = f'{RTP2HTTPD}/rtsp/{ex_url[len("rtsp://"):]}'
                    elif '/rtsp/' in ex_url:
                        # 已经是HTTP格式的rtsp地址
                        rtsp_url = ex_url
                    elif '/rtp/' in ex_url:
                        # 已经是HTTP格式的rtp地址
                        rtp_url = ex_url
                    else:
                        # 其他未知格式，原样使用
                        rtp_url = ex_url

                # 如果只有rtsp没有rtp，用rtsp URL当作rtp
                if rtp_url is None and rtsp_url is not None:
                    rtp_url = rtsp_url

                # 用转换后的rtp URL进行去重检查
                check_url = rtp_url if rtp_url else rtsp_url
                ex_mc = re.search(r'(239\.\d+\.\d+\.\d+:\d+)', check_url)
                ex_mc_addr = ex_mc.group(1) if ex_mc else None
                if check_url in used_urls or (ex_mc_addr and ex_mc_addr in used_mc_urls):
                    continue  # 播放地址或组播地址已存在，跳过

                # 写入sctv.txt（与爬取格式一致：name,rtspURL#rtpURL 或 name,rtpURL）
                if rtsp_url and rtp_url:
                    ftxt.write(f'{ex_name},{rtsp_url}#{rtp_url}\n')
                else:
                    ftxt.write(f'{ex_name},{rtp_url or rtsp_url}\n')

                # 写入sctv.m3u（外部频道同时有rtp和rtsp时都写上）
                tvg_logo = f'{logo_base}/logo/{ex_name}.png'
                if rtsp_url and rtp_url:
                    # 两种格式都有：先写rtp（主播放地址），再写rtsp（备用/时移地址）
                    fm3u.write(f'#EXTINF:-1 tvg-logo="{tvg_logo}" tvg-id="{ex_name}" tvg-name="{ex_name}", {ex_name}\n{rtp_url}\n{rtsp_url}\n')
                else:
                    # 只有一种格式，直接写
                    fm3u.write(f'#EXTINF:-1 tvg-logo="{tvg_logo}" tvg-id="{ex_name}" tvg-name="{ex_name}", {ex_name}\n{rtp_url or rtsp_url}\n')

                # 更新去重集合
                if rtp_url:
                    used_urls.add(rtp_url)
                if ex_mc_addr:
                    used_mc_urls.add(ex_mc_addr)
    # 生成频道信息映射字典（channel_id -> [name, user_channel_id]）
    for i, channel in enumerate(channels):
        channel_info[channel_ids[i][0]] = [channel_ids[i][1], channel_ids[i][2]]

    return channel_info  # 返回频道信息字典


# ==================== 频道列表获取函数 ====================
def get_channel_list(host: str, cookies: dict, user_token: str, stbid: str) -> Dict[str, List[str]]:
    """
    从服务器获取频道列表并处理
    :param host: 服务器主机地址
    :param cookies: 会话cookies
    :param user_token: 用户令牌
    :param stbid: 机顶盒ID
    :return: 频道信息字典
    """
    # 构造获取频道列表的URL
    channel_url = f'http://{host}/EPG/jsp/getchannellistHWCTC.jsp'
    # 构造请求数据
    channel_data = {
        'conntype': 'dhcp',          # 网络连接类型
        'UserToken': user_token,     # 用户令牌
        'tempKey': '',               # 临时密钥（空）
        'stbid': stbid,              # 机顶盒ID
        'SupportHD': '1',            # 支持高清
        'UserID': UserID,            # 用户ID
        'Lang': '1'                  # 语言设置
    }
    # 构造请求头
    headers = {
        'User-Agent': UserAgent,
        'Content-Type': 'application/x-www-form-urlencoded',
        'Referer': f'http://{host}/EPG/jsp/ValidAuthenticationHWCTC.jsp',
        'X-Requested-With': X_Requested_With,
    }
    # 发送POST请求获取频道列表（携带cookies）
    response = requests.post(channel_url, headers=headers, cookies=cookies, data=channel_data)
    response.raise_for_status()  # 检查响应状态

    # 记录响应内容到调试日志
    logger.debug(response.text)
    # 将响应内容保存到日志文件
    with open(os.getcwd() + "/log/getchannellistHWCTC.jsp", "w", encoding="utf-8") as f:
        f.write(response.text)

    # 使用正则表达式提取频道信息（匹配Channel字段中的所有字段）
    # 字段顺序: ChannelID, ChannelName, UserChannelID, ChannelURL(igmp), TimeShift, TimeShiftLength, ChannelSDP/TimeShiftURL, FCCEnable, ChannelFCCIP, ChannelFCCPort, ChannelFECPort
    pattern = re.compile(
        r'ChannelID\=\"(\d+)\",'          # 捕获组1: 频道ID
        r'ChannelName\=\"(.+?)\",'        # 捕获组2: 频道名称
        r'UserChannelID\=\"(\d+)\",'      # 捕获组3: 用户频道ID
        r'ChannelURL=\"igmp://(.+?)\".+?' # 捕获组4: IGMP组播地址
        r'TimeShift\=\"(\d+)\",'          # 捕获组5: 时移开关(0/1)
        r'TimeShiftLength\=\"(\d+)\".+?,' # 捕获组6: 时移时长(秒)
        r'TimeShiftURL\=\"(.+?)\".+?'     # 捕获组7: RTSP时移URL
        r'FCCEnable\=\"(\d+)\",'          # 捕获组8: FCC开关(0/1/2)
        r'ChannelFCCIP=\"(.*?)\",'        # 捕获组9: FCC服务器IP
        r'ChannelFCCPort=\"(.*?)\",'      # 捕获组10: FCC服务器端口
        r'ChannelFECPort=\"(.*?)\"'       # 捕获组11: FEC端口
    )

    # 执行正则匹配，返回所有匹配的频道数据列表
    channels = pattern.findall(response.text)
    # 处理频道数据并返回结果
    return process_channel_data(channels)


# ==================== EPG数据处理函数 ====================
def process_epg_data(channel_id: str, channel_name: List[str], response_text: str) -> Optional[List[Dict[str, str]]]:
    """
    处理EPG（电子节目指南）数据
    :param channel_id: 频道ID
    :param channel_name: 频道名称列表 [频道显示名, 用户频道ID]
    :param response_text: 服务器返回的EPG响应文本
    :return: 节目列表，每个元素为包含beginTime/endTime/programName的字典；解析失败返回None
    """
    # 使用正则表达式提取JavaScript数组格式的JSON数据
    match = re.search(r'parent\.jsonBackLookStr\s*=\s*(\[.*?\]);', response_text)
    if not match:
        logger.debug(f"无法从EPG响应中提取JSON数据，跳过频道 {channel_id}")
        return None

    try:
        # 解析JSON数据
        epg_data = json.loads(match.group(1))
        # 验证数据格式：必须是非空列表且至少包含2个子列表
        if not epg_data or not isinstance(epg_data, list) or len(epg_data) < 2:
            logger.debug(f"EPG数据为空或不完整，跳过频道 {channel_id}")
            return None

        # 解析日期列表和节目组列表
        day_list = epg_data[0] if epg_data[0] and isinstance(epg_data[0], list) else []
        program_groups = epg_data[1] if isinstance(epg_data[1], list) else []

        programs_data = []  # 初始化节目数据列表

        # 遍历每个节目组
        for group_index, programs in enumerate(program_groups):
            # 获取该组对应的日期索引（默认取今天的索引0）
            day_index = 0
            if group_index < len(day_list):
                day_info = day_list[group_index]
                if isinstance(day_info, dict):
                    # 从日期信息字典中获取index或dateIndex字段
                    day_index = day_info.get('index', day_info.get('dateIndex', 0)) or 0
            # 保留全部日期的节目数据（不再只保留今天和明天）
            # 只保留今天(index=0)和明天(index=1)的节目数据
            # (if day_index not in (0, 1):                continue)

            # 遍历节目组中的每个节目
            if isinstance(programs, list):
                for program in programs:
                    if not isinstance(program, dict):
                        continue

                    # 提取节目基本信息
                    program_data = {
                        'beginTimeFormat': program.get('beginTimeFormat', ''),
                        'endTimeFormat': program.get('endTimeFormat', ''),
                        'programName': program.get('programName', '未知节目')
                    }

                    # 验证时间格式是否有效
                    if not program_data['beginTimeFormat'] or not program_data['endTimeFormat']:
                        logger.debug(f"节目时间格式无效，跳过: {program_data}")
                        continue

                    # 格式化开始和结束时间
                    start_time = format_xmltv_time(program_data['beginTimeFormat'])
                    end_time = format_xmltv_time(program_data['endTimeFormat'])
                    if not start_time or not end_time:
                        logger.debug(f"时间转换失败，跳过: {program_data}")
                        continue

                    # 处理节目名称中的特殊字符（XML转义）
                    program_name = program_data["programName"]
                    program_name = program_name.replace('<', '《').replace('>', '》')  # 尖括号替换为书名号
                    program_name = program_name.replace('&', '&amp;')  # &符号XML转义

                    # 构建节目数据字典
                    programs_data.append({
                        'channel_name': clean_channel_name(channel_name[0]),  # 频道显示名称
                        'start_time': start_time,          # 开始时间
                        'end_time': end_time,              # 结束时间
                        'program_name': program_name       # 节目名称
                    })

        return programs_data  # 返回节目数据列表

    except (ValueError, json.JSONDecodeError, KeyError) as e:
        # 捕获JSON解析错误、键不存在等异常
        logger.error(f"解析EPG JSON数据失败: {str(e)}")
        return None


# ==================== EPG文件写入函数 ====================
def write_epg_file(channel_info: Dict[str, List[str]], epg_data: Dict[str, List[Dict[str, str]]]):
    """
    将EPG数据写入XMLTV格式的xml.gz压缩文件
    :param channel_info: 频道信息字典 {channel_id: [display_name, user_channel_id]}
    :param epg_data: EPG节目数据字典 {channel_id: [节目列表]}
    """
    # 先构建完整的XML内容（字符串列表形式）
    xml_lines = []
    # 写入XML声明（指定UTF-8编码）
    xml_lines.append('<?xml version="1.0" encoding="UTF-8"?>')
    # 写入根元素开始标签
    xml_lines.append('<tv>')
    # 遍历所有频道，写入频道信息
    for channel_id, channel_name in channel_info.items():
        # 写入channel开始标签（使用频道ID作为id属性）
        xml_lines.append(f'  <channel id="{clean_channel_name(channel_name[0])}">')
        # 写入显示名称
        xml_lines.append(f'    <display-name lang="zh">{clean_channel_name(channel_name[0])}</display-name>')
        # 写入channel结束标签
        xml_lines.append('  </channel>')
    # 遍历所有频道的节目数据
    for channel_id, programs in epg_data.items():
        # 遍历每个频道的每个节目
        for program in programs:
            # 写入programme标签（包含频道、开始时间、结束时间属性）
            xml_lines.append(f'  <programme channel="{clean_channel_name(program["channel_name"])}" '
                           f'start="{program["start_time"]} +0800" '
                           f'stop="{program["end_time"]} +0800">')
            # 写入节目标题
            xml_lines.append(f'    <title lang="zh">{program["program_name"]}</title>')
            # 写入节目描述（空）
            xml_lines.append(f'    <desc lang="zh"></desc>')
            # 写入programme结束标签
            xml_lines.append('  </programme>')
    # 写入根元素结束标签
    xml_lines.append('</tv>')
    # 将XML内容拼接为完整字符串
    xml_content = '\n'.join(xml_lines)
    # 使用gzip以二进制写模式打开文件（compresslevel=9为最高压缩率）
    # 输出文件名为 epg.xml.gz（.gz后缀表示gzip压缩格式）
    output_path = os.getcwd() + '/output/epg.xml.gz'
    with gzip.open(output_path, 'wb', compresslevel=9) as f:
        # 将字符串编码为UTF-8字节流写入gzip压缩流
        f.write(xml_content.encode('utf-8'))
    logger.info(f"EPG文件已保存为gzip压缩格式: {output_path}")


# ==================== EPG数据获取函数 ====================
def get_epg(host: str, cookies: dict, channel_info: Dict[str, List[str]],
            user_token: str, stbid: str):
    """
    获取所有频道的EPG数据并写入文件
    :param host: 服务器主机地址
    :param cookies: 会话cookies
    :param channel_info: 频道信息字典
    :param user_token: 用户令牌（从认证流程获取）
    :param stbid: 机顶盒ID（从认证流程获取）
    """
    epg_data = {}  # 初始化EPG数据字典

    # 遍历所有频道获取EPG
    for channel_id, channel_name in channel_info.items():
        logger.info(f"正在获取频道 {channel_name[0]} 的EPG数据")

        # 构造EPG请求URL（携带频道ID参数）
        epg_url = f'http://{host}/EPG/jsp/tools/aged/getTvodData.jsp?channelId={channel_id}'
        # 构造请求头
        headers = {
            'User-Agent': UserAgent,
            'Content-Type': 'application/x-www-form-urlencoded',
            'Referer': f'http://{host}/EPG/jsp/tools/aged/getTvodData.jsp'
        }
        # 构造请求数据
        data = {
            'conntype': '',
            'UserToken': user_token,     # 使用传入的user_token参数
            'tempKey': '',
            'stbid': stbid,              # 使用传入的stbid参数
            'SupportHD': '1',
            'UserID': UserID,
            'Lang': '1'
        }

        try:
            # 发送POST请求获取EPG数据（超时10秒）
            response = requests.post(epg_url, headers=headers, data=data, cookies=cookies, timeout=10)
            response.raise_for_status()  # 检查响应状态

            # 处理EPG响应数据
            programs = process_epg_data(channel_id, channel_name, response.text)
            if programs:
                epg_data[channel_id] = programs  # 保存解析后的节目数据

        except requests.exceptions.RequestException as e:
            # 捕获网络请求异常
            logger.error(f"获取频道 {channel_name[0]} 的EPG数据失败: {str(e)}")
            continue  # 跳过当前频道，继续处理下一个

    # 将所有EPG数据写入XML文件
    write_epg_file(channel_info, epg_data)
    logger.info('epg.xml.gz, sctv.m3u, sctv.txt 已保存到output目录')


# ==================== EPG Git同步函数 ====================
def sync_epg():
    """
    将epg.xml同步到Git仓库
    流程：克隆仓库 -> 复制文件 -> 提交 -> 强制推送 -> 清理临时目录
    """
    # 从配置中获取Git仓库信息
    epg_git_repo = config.get('epg_git_repo')
    epg_git_private_token = config.get('epg_git_private_token')

    # 检查是否配置了Git仓库信息
    if not epg_git_repo or not epg_git_private_token:
        logger.warning("未配置EPG Git仓库信息，跳过同步")
        return

    try:
        # 构建带token的仓库URL（将https://替换为https://token@）
        if epg_git_repo.startswith('https://'):
            repo_url = epg_git_repo.replace('https://', f'https://{epg_git_private_token}@')
        else:
            raise ValueError("目前仅支持HTTPS格式的Git仓库URL")

        # 本地git仓库目录路径
        git_dir = os.path.join(os.getcwd(), "epg_repo")

        # 如果本地仓库目录已存在，先删除清理
        if os.path.exists(git_dir):
            shutil.rmtree(git_dir)

        # 步骤1：克隆远程仓库到本地
        logger.info(f"正在克隆仓库...")
        subprocess.run(["git", "clone", repo_url, git_dir], check=True, capture_output=True)

        # 步骤2：配置git用户信息
        subprocess.run(["git", "config", "user.name", "iptv-bot"], cwd=git_dir, check=True)
        subprocess.run(["git", "config", "user.email", "iptv-bot@local"], cwd=git_dir, check=True)

        # 步骤3：复制epg.xml到git仓库目录
        src_file = os.path.join(os.getcwd(), "output", "epg.xml.gz")
        dst_file = os.path.join(git_dir, "epg.xml.gz")

        if not os.path.exists(src_file):
            logger.error(f"源文件 {src_file} 不存在")
            return

        shutil.copy2(src_file, dst_file)  # 复制文件（保留元数据）

        # 步骤4：提交更改
        # 检查仓库是否有历史提交记录
        has_commit = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=git_dir, capture_output=True
        ).returncode == 0

        if has_commit:
            # 尝试回退上一次提交（--soft模式保留文件在暂存区）
            try:
                # 检查是否有至少一个提交
                commit_count = subprocess.run(
                    ["git", "rev-list", "--count", "HEAD"],
                    cwd=git_dir, capture_output=True, text=True
                ).stdout.strip()
                if int(commit_count) > 0:
                    subprocess.run(
                        ["git", "reset", "--soft", "HEAD^"],
                        cwd=git_dir, check=True, capture_output=True
                    )
                    logger.info("已撤销上一次提交 (reset --soft HEAD^)")
            except Exception:
                pass  # 回退失败不影响后续流程

        # 添加所有更改到暂存区
        subprocess.run(["git", "add", "."], cwd=git_dir, check=True)

        # 提交更改并附带时间戳注释
        timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        subprocess.run(
            ["git", "commit", "-m", f"Update EPG {timestamp}"],
            cwd=git_dir, check=True
        )

        # 步骤5：强制推送到远程仓库
        logger.info("正在推送到远程仓库...")
        subprocess.run(
            ["git", "push", repo_url, "-f"],
            cwd=git_dir, check=True, capture_output=True
        )

        logger.info("EPG数据同步成功")

    except subprocess.CalledProcessError as e:
        # 捕获git命令执行失败
        err_msg = e.stderr.decode() if e.stderr else str(e)
        # 从错误信息中脱敏Git令牌
        if epg_git_private_token in err_msg:
            err_msg = err_msg.replace(epg_git_private_token, "******")
        logger.error(f"Git操作失败: {err_msg}")

    except Exception as e:
        # 捕获其他未知异常
        logger.error(f"同步EPG数据失败: {str(e)}")

    finally:
        # 无论成功还是失败，都清理临时git目录
        if os.path.exists(git_dir):
            try:
                shutil.rmtree(git_dir)
            except Exception:
                pass


# ==================== 程序入口 ====================
if __name__ == '__main__':
    # 执行认证流程，获取host、cookies、user_token、stbid
    host, cookies, user_token, stbid = get_auth()
    logger.info(f"认证结果：host={host}, cookies={cookies}, user_token={user_token}, stbid={stbid}")

    # 检查认证是否成功
    if not host:
        print("认证失败，无法获取host地址")
        exit(1)  # 认证失败则退出程序

    # 获取频道列表
    channel_info = get_channel_list(host, cookies, user_token, stbid)
    # 获取并处理EPG数据（传入user_token和stbid参数）
    get_epg(host, cookies, channel_info, user_token, stbid)
    # 同步EPG数据到Git仓库
    sync_epg()
