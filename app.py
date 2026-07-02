#!/usr/bin/env python3
"""
毒霸弹泡反馈展示系统 v2
"""
import json
import os
import re
import hashlib
from datetime import date, timedelta
from flask import Flask, jsonify, request, render_template_string
from flask_cors import CORS
import pymysql

app = Flask(__name__)
CORS(app)

DB_CONFIG = {
    'host': '124.71.26.25',
    'port': 3306,
    'user': 'user_analy_yxf',
    'password': '0EPdWdrOBY!F4JwcQI',
    'database': 'infoc_report',
    'charset': 'utf8mb4',
    'cursorclass': pymysql.cursors.DictCursor
}

CATEGORIES = [
    "被捆绑安装，反复安装",
    "不好用、不想用、不需要",
    "弹窗广告",
    "功能问题",
    "其他",
    "其他原因",
    "软件兼容问题",
    "收费原因",
    "推广安装其他软件、插件",
    "误删误拦",
    "修改默认",
    "影响其他软件",
    "用竞品",
    "占用高，导致电脑卡慢",
    "谩骂"
]

_classify_cache = {}
CACHE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'classify_cache.json')
CORRECTION_LOG = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'correction_log.jsonl')
INVALID_LOG = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'invalid_log.jsonl')
LEARNED_RULES_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'learned_rules.json')

# ── 学习规则（每周五更新，启动时加载）──────────────────────────────────────────
_learned_exact_mappings: dict = {}   # word -> 正确分类
_learned_add_keywords: dict = {}     # 分类 -> [额外关键词]
_learned_remove_keywords: dict = {}  # 分类 -> [排除词]
_learned_invalid_blacklist: set = set()  # 精确无效反馈黑名单

def load_learned_rules():
    """从 learned_rules.json 加载学习规则"""
    global _learned_exact_mappings, _learned_add_keywords, _learned_remove_keywords, _learned_invalid_blacklist
    if not os.path.exists(LEARNED_RULES_FILE):
        return
    try:
        with open(LEARNED_RULES_FILE, 'r', encoding='utf-8') as f:
            data = json.load(f)
        _learned_exact_mappings = data.get('exact_mappings', {})
        _learned_add_keywords = data.get('add_keywords', {})
        _learned_remove_keywords = data.get('remove_keywords', {})
        _learned_invalid_blacklist = set(data.get('invalid_blacklist', []))
        meta = data.get('_meta', {})
        print(f"[learned_rules] 已加载：精确映射 {len(_learned_exact_mappings)} 条，"
              f"新增关键词分类 {len(_learned_add_keywords)} 类，"
              f"无效黑名单 {len(_learned_invalid_blacklist)} 条"
              f"（最后更新：{meta.get('last_updated', '未知')}）")
    except Exception as e:
        print(f'[learned_rules] 加载失败: {e}')

# 按日期缓存的查询结果（历史日期缓存，当天不缓存）
FEEDBACK_CACHE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'feedback_cache')
os.makedirs(FEEDBACK_CACHE_DIR, exist_ok=True)


def _feedback_cache_path(date_str: str) -> str:
    return os.path.join(FEEDBACK_CACHE_DIR, f'{date_str}.json')


def _load_feedback_cache(date_str: str):
    """加载某日期的缓存，返回 dict 或 None"""
    path = _feedback_cache_path(date_str)
    if not os.path.exists(path):
        return None
    try:
        with open(path, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return None


def _save_feedback_cache(date_str: str, data: dict):
    """保存某日期的查询结果到缓存文件"""
    path = _feedback_cache_path(date_str)
    try:
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False)
    except Exception as e:
        print(f'[feedback_cache] write error: {e}')

# 手动标记无效反馈的黑名单（内存，启动时从日志加载）
_manual_invalid: set = set()

def load_invalid_log():
    global _manual_invalid
    if not os.path.exists(INVALID_LOG):
        return
    with open(INVALID_LOG, 'r', encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                e = json.loads(line)
                if e.get('action') == 'mark_invalid':
                    _manual_invalid.add(e['word'])
                elif e.get('action') == 'mark_valid':
                    _manual_invalid.discard(e['word'])
            except:
                pass

load_invalid_log()


def log_correction(word: str, old_category: str, new_category: str):
    """记录手动纠正日志，JSONL 格式，每行一条"""
    import datetime
    entry = {
        'ts': datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        'word': word,
        'old': old_category,
        'new': new_category,
    }
    try:
        with open(CORRECTION_LOG, 'a', encoding='utf-8') as f:
            f.write(json.dumps(entry, ensure_ascii=False) + '\n')
    except Exception as e:
        print(f'[correction_log] write error: {e}')

def load_cache():
    global _classify_cache
    if os.path.exists(CACHE_FILE):
        try:
            with open(CACHE_FILE, 'r', encoding='utf-8') as f:
                _classify_cache = json.load(f)
        except:
            _classify_cache = {}

def save_cache():
    try:
        with open(CACHE_FILE, 'w', encoding='utf-8') as f:
            json.dump(_classify_cache, f, ensure_ascii=False, indent=2)
    except:
        pass

load_cache()
load_learned_rules()


def is_valid_feedback(word: str) -> bool:
    """
    有效反馈判断：必须包含至少一个汉字或英文字母，且排除明显无意义内容
    """
    if not word:
        return False
    word = word.strip()
    if not word or word.upper() in ('NULL', 'N/A', 'NA', '-', '无', '没有'):
        return False
    # 手动标记无效黑名单（优先级最高）
    if word in _manual_invalid:
        return False
    # 学习规则：自动更新的无效反馈黑名单
    if word in _learned_invalid_blacklist:
        return False
    # 精确黑名单（用户手动标记的无效反馈）
    _exact_blacklist = {'tyufiftiy', 'i sh', 'afda', '等等人', '是哒哒哒哒', '可以看出有空', 'v没南方南方你', '键盘【基'}
    if word.strip().lower() in _exact_blacklist:
        return False
    # 核心规则：必须包含汉字或英文字母
    if not re.search(r'[a-zA-Z\u4e00-\u9fff]', word):
        return False
    # 长度过短（单个字母/字符）
    if len(word) <= 1:
        return False

    # ── 乱码/键盘乱敲过滤 ──
    chinese_chars = re.findall(r'[\u4e00-\u9fff]', word)
    alpha_chars = re.findall(r'[a-zA-Z]', word)
    total_len = len(word)

    # 1. 字母+符号乱敲：汉字极少（≤2个），去掉非字母非汉字后，字母占净内容>55%
    #    例：dfgd / mmkpi0-i= 7p-o=uu0 / asdfghjkl
    if len(chinese_chars) <= 2:
        # 提取所有连续英文片段
        words_found = re.findall(r'[a-zA-Z]{3,}', word)
        # 判定为"真实英文"：含元音、长度合理(≤12)、无连续重复(ffff...)、
        #   且不是键盘行顺序（qwerty/asdf/zxcv 等常见乱敲模式）
        keyboard_rows = re.compile(
            r'(qwert|werty|ertyu|rtyui|tyuio|yuiop'
            r'|asdfg|sdfgh|dfghj|fghjk|ghjkl'
            r'|zxcvb|xcvbn|cvbnm'
            r'|qazws|wsxed|edcrf|rfvtg|tgbyh|yhnuj|ujmik|ikolp)',
            re.IGNORECASE
        )
        valid_english = [w for w in words_found
                         if re.search(r'[aeiouAEIOU]', w)
                         and len(w) <= 12
                         and not re.search(r'(.)\1{3,}', w)
                         and not keyboard_rows.search(w)]
        if len(alpha_chars) >= 4 and not valid_english:
            return False

    # 1b. 字母被符号/数字/空格切碎的乱敲：汉字≤2，字母总数≥4
    #     条件：最长字母段≤7 且 平均段长≤4 且 无有效英文单词（含元音、长度4+）
    #     例：mmkpi0-i= 7p-o=uu0 / utud nk ydkmhgh z
    if len(chinese_chars) <= 2 and len(alpha_chars) >= 4:
        alpha_segments = re.findall(r'[a-zA-Z]+', word)
        if alpha_segments:
            max_seg = max(len(s) for s in alpha_segments)
            avg_seg = sum(len(s) for s in alpha_segments) / len(alpha_segments)
            def looks_like_real_word(s):
                if len(s) < 4:
                    return False
                if not re.search(r'[aeiouAEIOU]', s):
                    return False
                if re.search(r'(.)\1{2,}', s):  # 连续重复3次+（mmm/ooo）才算乱码
                    return False
                if keyboard_rows.search(s):
                    return False
                # 短词（≤4字符）：必须是常见词或缩写才认可
                SHORT_WHITELIST = {
                    'cpu', 'gpu', 'ram', 'usb', 'vpn', 'app', 'pc', 'ok', 'no',
                    'not', 'too', 'bad', 'good', 'use', 'can', 'win', 'mac',
                    'the', 'and', 'for', 'are', 'this', 'that', 'with',
                }
                if len(s) <= 4 and s.lower() not in SHORT_WHITELIST:
                    return False
                # 长词：辅音占比超75%视为乱码
                consonant_ratio = len(re.findall(r'[bcdfghjklmnpqrstvwxyzBCDFGHJKLMNPQRSTVWXYZ]', s)) / len(s)
                if consonant_ratio > 0.75:
                    return False
                return True
            valid_words = [s for s in alpha_segments if looks_like_real_word(s)]
            if max_seg <= 7 and avg_seg < 4.0 and not valid_words:
                return False

    # 2. 存在超长随机字母串（单段>10字符且含重复字符或无元音）
    #    例：gffffffffjklgerkjnhtr / yttrnhtrt
    long_alpha_segments = re.findall(r'[a-zA-Z]{10,}', word)
    for seg in long_alpha_segments:
        has_vowel = bool(re.search(r'[aeiouAEIOU]', seg))
        has_repeat = bool(re.search(r'(.)\1{3,}', seg))  # 连续重复4次+
        if not has_vowel or has_repeat:
            return False

    # 3. 汉字极少（≤3个）但总长度很长（>15字符），且大量非汉字字符
    #    例：放到后台日本头发gffffffffjklgerkjnhtr mk,rhgliohtrjlk6yoi6i
    if len(chinese_chars) <= 3 and total_len > 15:
        non_chinese_ratio = 1 - len(chinese_chars) / total_len
        if non_chinese_ratio > 0.75:
            # 再检查英文部分是否像真实内容
            words_found = re.findall(r'[a-zA-Z]{4,}', word)
            valid_english = [w for w in words_found
                             if re.search(r'[aeiouAEIOU]', w)
                             and not re.search(r'(.)\1{3,}', w)
                             and len(w) <= 12]
            if len(alpha_chars) > 5 and not valid_english:
                return False

    # 4. 纯汉字但与毒霸/软件完全无关的乱填内容
    #    判断：≥8个汉字、无英文字母、不含任何软件/卸载相关词
    #    例：广阔的快乐反过来看到福建高考了尽量快点发几个了快递费
    if len(chinese_chars) >= 8 and len(alpha_chars) == 0:
        duba_related = [
            '软件', '毒霸', '安全', '杀毒', '防护', '卸载', '安装', '会员', '收费',
            '弹窗', '广告', '卡', '慢', '捆绑', '拦截', '删除', '插件', '驱动',
            '系统', '电脑', '功能', '版本', '更新', '登录', '账号',
            '不好', '不用', '不需', '不想', '影响', '占用', '兼容', '崩溃',
            '竞品', '火绒', '360', '管家', '主页', '默认', '浏览器', '误',
            '推广', '捆', '弹', '卡顿', '内存', '流氓', '骚扰', '打扰',
        ]
        if not any(kw in word for kw in duba_related):
            return False

    # 5. 短汉字句（≤5个汉字）且无英文字母，语义与软件/卸载完全无关
    #    例：感觉预热天涯、看看健康、刚刚突然、少羽牛逼、阿斯蒂芬
    if 2 <= len(chinese_chars) <= 5 and len(alpha_chars) == 0 and total_len <= 8:
        duba_related_short = [
            '软件', '毒霸', '安全', '杀毒', '防护', '卸载', '安装', '会员', '收费',
            '弹窗', '广告', '捆绑', '拦截', '删除', '插件', '驱动', '系统', '电脑',
            '功能', '不好', '不用', '不需', '不想', '影响', '占用', '兼容', '崩溃',
            '浏览器', '误', '推广', '流氓', '骚扰', '打扰', '垃圾', '卡', '慢',
            '钱', '充值', '收费', '免费', '会员', '登录', '密码', '账号',
        ]
        if not any(kw in word for kw in duba_related_short):
            return False

    # 6. 重复汉字（如：灌灌灌灌灌、哈哈哈哈、啊啊啊啊啊）
    if re.search(r'([\u4e00-\u9fff])\1{3,}', word):
        return False

    return True


def rule_classify(text: str) -> str:
    """
    基于规则的分类，规则按优先级从高到低排列。
    原则：
      1. 谩骂类（纯情绪、无实质诉求）优先识别
      2. 具体诉求类关键词尽量细化，减少落入"其他"
      3. 同一文本若含多个特征，取最先匹配的规则
    v2: 根据纠正日志优化，增加拼音/英文识别、推广/竞品/影响其他等规则覆盖
    v3: 支持从 learned_rules.json 加载学习规则（精确映射 + 动态关键词）
    """
    # ── 优先级 0：精确映射（来自人工纠正学习）────────────────────────────────
    if text.strip() in _learned_exact_mappings:
        return _learned_exact_mappings[text.strip()]
    # ── 拼音/英文 → 优先转换为可识别关键词后再分类
    # 常见拼音/英文反馈的映射表（全小写匹配）
    PINYIN_MAP = {
        'buxiangyong': '不想用', 'buyaoyong': '不想用', 'buyao': '不要',
        'buhaoyong': '不好用', 'buhao': '不好', 'bu hao yong': '不好用',
        'kadun': '卡顿', 'taikale': '太卡了', 'kade': '卡', 'taimanle': '太慢了',
        'laji': '垃圾', 'lajishijian': '垃圾', 'la ji': '垃圾',
        'shoufei': '收费', 'shonfei': '收费', 'meiqain': '没钱', 'meiqian': '没钱',
        'shabishenmedouyaohuiyuan,qusiba': '傻逼都要会员去死吧',
        'buxiang': '不想',
        'BUXIANGYONG': '不想用', 'BUYAOYONG': '不想用',
        'BUXIANGYONG': '不想用',
    }
    _text_lower = text.strip().lower()
    if _text_lower in PINYIN_MAP:
        text = PINYIN_MAP[_text_lower]
    elif text.strip() in PINYIN_MAP:
        text = PINYIN_MAP[text.strip()]

    # ── 1. 谩骂（纯骂人/泄愤，无实质诉求）
    # 先检测：文本极短且只含骂人词，或整体以骂人为主
    abuse_kws = [
        "你妈", "你全家", "去死", "操你", "草泥马", "草拟吗", "妈的", "我操",
        "傻逼", "傻*", "煞笔", "傻比", "sb", "nmb", "cnm", "wdnmd", "滚", "fuck",
        "流氓软件", "流氓", "垃圾软件", "狗东西", "你们去死", "死流氓",
        "狗屎", "狗玩意", "臭东西", "烂东西", "垃圾东西", "什么玩意", "什么鬼",
        "坑爹", "坑人", "骗子", "恶心", "恶臭", "臭软件", "烂软件",
        "死全家", "丢雷老母", "你姥", "你大爷",
    ]
    # 谩骂短句：纯骂人词组成、没有其他具体诉求
    abuse_only_kws = [
        "你妈的", "你全家", "去你妈", "去死", "我操你", "草泥马", "草拟吗",
        "傻逼", "煞笔", "nmb", "cnm", "wdnmd", "你们怎么不去死",
        "全家死", "死全家", "不要脸", "他妈的", "妈了个", "sb",
        "狗屎玩意", "狗屎", "狗玩意", "什么垃圾", "垃圾玩意", "傻比软件",
        "丢雷老母",
    ]

    # ── 2. 推广安装其他软件、插件
    # ★ 含义：金山毒霸捆绑/推广安装了其他软件（毒霸是主动方）
    promo_kws = [
        "装了其他", "附带安装", "顺带装", "捆着装", "插件",
        "推广软件", "推广安装",
        "还装了", "顺便装了", "绑定了其他", "带着装",
        "安装其他软件", "未经提示安装", "私自安装其他", "自动安装其他",
        "偷偷安装其他", "强制安装其他", "未告知安装",
        # 新增：乱装/任意装/给我装软件
        "乱给我安装", "任意给安装", "随意给安装", "给我安装软件",
        "乱装软件", "装了一堆", "装了一堆你们",
        # 推荐其他软件（含"推荐+软件名"）
        "推荐猎豹", "推荐其他软件", "建议去除",
    ]

    # ── 3. 被捆绑安装/反复安装
    # ★ 含义：用户被动装上了金山毒霸
    bundle_kws = [
        "捆绑", "强制安装", "反复安装", "自动安装", "偷偷安装", "乱装",
        "没有安装过", "不知道啥时候装上", "莫名其妙下载", "自己装上来的",
        "自动下载", "自动给我装",
        # 未经同意/未经允许
        "未经同意", "未经允许", "没有同意", "擅自安装", "私自安装",
        "随意安装", "不是我自己", "没让它装", "强行安装",
        "强制给我装", "被安装", "不知情", "没有经过我",
        # 装了其他金山产品后被顺带安装毒霸
        "金山打字通", "金山词霸", "金山文档", "金山软件",
        "就被下载了", "就自动装了", "跟着装了",
        # 后台偷装
        "后台给我安装", "后台安装", "在后台装", "后台下载", "后台给我下",
        "莫名其妙被安装", "莫名被安装",
    ]

    # ── 4. 修改默认
    default_kws = [
        "主页", "默认浏览器", "首页", "篡改", "改了我", "强制设置",
        "默认打开方式", "修改浏览器", "浏览器主页", "改主页", "默认页面",
        # 新增：自作主张把浏览器改为XXX
        "自作主张", "把浏览器改", "浏览器改为", "改成了浏览器",
    ]

    # ── 5. 弹窗广告
    popup_kws = [
        "弹窗", "广告", "弹出", "骚扰", "打扰", "频繁弹", "一直弹",
        "弹广告", "弹提示", "经常弹", "屏保", "弹跳屏保",
        # 注意：去掉"推广太多/推广过多/推广乱"，这些应归推广安装
        "干扰太多", "一直有弹窗", "频繁广告",
    ]

    # ── 6. 占用高/卡慢
    perf_kws = [
        "卡顿", "电脑卡", "电脑慢", "cpu高", "内存占用", "占用高",
        "cpu占用", "资源占用", "性能差", "lag", "电脑变慢", "运行慢",
        "内存", "cpu", "占用", "资源",
        "开机缓慢", "开机慢", "启动慢",
    ]

    # ── 7. 收费
    fee_kws = [
        "会员", "收费", "付费", "要钱", "价格", "太贵", "vip",
        "收钱", "花钱", "不免费", "不缴费", "不是免费", "要消费",
        "缴费", "要付钱", "需要钱", "shoufei",
        "免费软件", "喜欢免费", "用免费", "学生党", "囊中羞涩", "经济", "买不起",
        "交不起钱", "交不起", "没钱", "用不起",
        # 新增
        "到处需要充值", "充值", "都要充值", "都要收费", "需要充钱",
        "穷", "太贵了", "带费用", "有费用", "需要费用",
        "expensive", "too expensive",
    ]

    # ── 8. 软件兼容问题
    compat_kws = [
        "兼容", "冲突", "蓝屏", "崩溃", "报错", "异常", "不稳定", "死机",
        "影响装系统", "装系统", "重装", "系统出问题",
    ]

    # ── 9. 影响其他软件
    affect_kws = [
        "影响其他软件", "影响其他", "干扰其他", "其他软件用不了",
        "影响别的", "正常软件", "其他软件无法", "阻止我", "干扰我",
        "看不了", "看视频", "电影看不了",
        # 新增：影响下载考试端/银行证书等
        "影响下载", "影响考试", "无法安装", "银行证书", "证书软件",
        "影响电脑功能",
    ]

    # ── 10. 误删误拦
    misblock_kws = [
        "误删", "误拦", "误报", "误杀", "误判", "误操作",
        "拦截了我", "把.*删了", "删了我的", "拦我",
        "错误地拦截", "错误拦截", "错误删除", "错误地删",
        "拦截了", "拦截我的", "误拦截",
        # 新增：乱杀文件
        "乱杀文件", "乱删文件", "乱杀", "乱删",
    ]

    # ── 11. 用竞品
    # ★ 含义：已有其他安全软件，不需要毒霸
    rival_kws = [
        "360", "火绒", "腾讯电脑管家", "电脑管家", "安全卫士",
        "用别的", "换了别的", "用其他的",
        "雷军",
        "软件重复", "功能重复", "重复了", "已有杀毒", "已经有了",
        "有了别的", "用其他杀毒", "已有安全软件",
        "自带管家", "系统自带", "电脑自带",
        # 新增：已有相关/类似/相同软件
        "已有相关软件", "有相同类型软件", "杀毒软件太多了", "有相同软件",
        "有类似软件", "有相似软件", "杀毒太多",
    ]

    # ── 12. 不需要
    no_need_kws = [
        "不需要了", "用不到", "不想要了", "没用", "闲置", "多余",
        "不用了", "不想安装", "不需要这个", "不装了",
        "并不需用", "并不需要",
    ]

    # ── 13. 功能问题
    func_kws = [
        "功能", "缺少", "不支持", "没有这个功能", "无法启动",
        "扫描二维码", "必须登录", "强制登录", "登录才能",
        "漏洞修复", "修复漏洞",
        # 新增：关不了/没拦截好/程序错误
        "关不了", "没有拦截好", "没拦截好", "程序错误",
        "无法关闭", "无法退出", "退出不了",
        # 注意：不含"要登录"（可能是收费原因），不含"无法使用"/"不能用"（太泛）
    ]

    # ── 14. 不好用
    bad_kws = [
        "不好用", "难用", "体验差", "不喜欢", "不满意", "差劲",
        "太差", "做得很差", "很烂", "烂软件", "用着不顺手",
        "不想用", "不用了", "不想再用", "不想继续用", "用不下去",
        "不想使用", "不再使用", "不想继续使用", "不愿使用", "放弃使用",
        "不使用了", "用不惯", "不习惯用",
    ]

    # ── 谩骂判断逻辑：
    # 收费相关词（优先级高，要在谩骂之前识别）
    has_fee = any(kw in text.lower() for kw in fee_kws)
    has_specific = any(kw in text for kw in (
        promo_kws + bundle_kws + default_kws + popup_kws + perf_kws +
        compat_kws + affect_kws + misblock_kws + rival_kws +
        no_need_kws + func_kws + bad_kws
    )) or has_fee or bool(re.search(
        r"(清理.*登录|登录.*钱|充值|收费|免费|会员|vip|带费用|有费用|expensive)",
        text, re.IGNORECASE
    ))

    # 谩骂短句：含骂人短句且无具体诉求
    has_abuse_phrase = any(kw in text.lower() for kw in abuse_only_kws)
    # 通用骂人词（配合"纯骂"语境）
    has_abuse_generic = any(kw in text.lower() for kw in [
        "流氓软件", "垃圾软件", "狗东西", "你就是病毒", "本身就是毒",
        "死全家", "丢雷老母",
    ])

    if has_abuse_phrase and not has_specific:
        return "谩骂"
    # 短文本纯含通用骂词也归谩骂
    if has_abuse_generic and not has_specific and len(text) <= 20:
        return "谩骂"

    # ── 按优先级逐条匹配

    # 推广安装（优先于捆绑：含"推广"类意图）
    if any(kw in text for kw in promo_kws):
        return "推广安装其他软件、插件"
    # 推广相关正则
    if re.search(r"(总是|经常|乱|随意|任意).{0,6}(给|帮|替).{0,4}(安装|下载|装上)", text):
        return "推广安装其他软件、插件"
    if re.search(r"(推荐|建议去除|强推).{0,8}(软件|程序|插件|猎豹|PDF|工具)", text):
        return "推广安装其他软件、插件"
    # "推广太多/过多" → 推广安装（不是弹窗）
    if re.search(r"推广.{0,4}(太多|过多|乱|频繁)", text):
        return "推广安装其他软件、插件"

    if any(kw in text for kw in bundle_kws):
        return "被捆绑安装，反复安装"

    # 修改默认（支持正则）
    if any(kw in text for kw in ["主页", "默认浏览器", "首页", "篡改", "改了我", "强制设置", "默认页面", "默认打开", "改主页", "修改浏览器", "自作主张", "把浏览器改", "浏览器改为"]):
        return "修改默认"
    if re.search(r"默认.{0,5}打开|打开.{0,5}默认", text):
        return "修改默认"
    # 新增：打开chrome就跳到duba网站
    if re.search(r"打开.{0,10}(chrome|浏览器).{0,15}(跳|跳转|跳到|duba|毒霸)", text, re.IGNORECASE):
        return "修改默认"

    if any(kw in text for kw in popup_kws):
        return "弹窗广告"
    # 新增弹窗正则
    if re.search(r"(一直|总是|经常|频繁|不停).{0,4}(弹|弹出|广告|骚扰|打扰)", text):
        return "弹窗广告"

    # 误删误拦优先（防止"驱动被删/声卡删掉"被性能规则误中"卡"字）
    if any(kw in text for kw in misblock_kws) or re.search(r"(驱动|文件|数据|声卡|网卡).{0,5}(被删|删掉|给删|删了|删除)", text):
        return "误删误拦"

    # "无法登录会员" → 功能问题（优先于收费判断）
    if re.search(r"(无法|不能|登录不了).{0,4}(登录|会员账号)", text) and '会员' in text and re.search(r"无法|不能|登录不了", text):
        return "功能问题"
    # 登录+会员：区分"无法登录会员"(功能) vs "要登录才能用会员"(收费)
    if re.search(r"无法登录会员|不能登录会员|会员登录.{0,4}(不了|失败|无法)", text):
        return "功能问题"

    # 收费原因优先于性能（防止"内存"被误判，如"清个内存居然要钱"）
    if has_fee:
        return "收费原因"
    # 收费相关正则补充
    if re.search(r"(想钱|要钱|就知道.{0,4}钱|钱.{0,4}疯了|冲钱|充钱)", text):
        return "收费原因"
    if re.search(r"钱{2,}", text):
        return "收费原因"
    if re.search(r"(喜欢|用|要|想用|找|选).{0,6}免费|免费的.{0,4}(软件|产品)|学生.{0,6}(免费|没钱|省钱)", text):
        return "收费原因"
    if re.search(r"(我是|囊中|经济|).{0,4}(穷|穷逼|穷人|没钱|买不起|用不起)", text):
        return "收费原因"
    # "清理/功能 + 需要登录/要登录" → 收费（功能被锁在登录墙后）
    if re.search(r"(清理|功能|使用|查杀).{0,6}(还有|需要|要|都要).{0,4}登录", text):
        return "收费原因"

    # 性能/卡慢
    if any(kw in text.lower() for kw in perf_kws) or re.search(r"(太|好|很|有点|特别)?(阿?卡|卡顿|变慢|好慢|太慢|卡死)", text):
        return "占用高，导致电脑卡慢"

    if any(kw in text for kw in compat_kws):
        return "软件兼容问题"

    # 程序错误/软件出错 → 功能问题（优先于影响其他软件）
    if re.search(r"程序.{0,4}(错误|异常|出问题|出错)", text):
        return "功能问题"

    # 影响其他软件（扩充：无法打开XX/XX打不开/XX用不了）
    if any(kw in text for kw in affect_kws) or re.search(r"(无法|打不开|用不了|登录不了|无法打开).{0,10}(浏览器|微信|QQ|软件|程序|应用)", text):
        return "影响其他软件"
    # 新增：无法安装特定软件
    if re.search(r"(无法|不能).{0,6}(安装|使用|打开).{0,10}(软件|证书|考试|银行|程序)", text):
        return "影响其他软件"

    # 竞品（放在"用竞品"关键词之前先做精确正则）
    if re.search(r"(有|装了|用了|已有|自带).{0,6}管家|管家.{0,6}(有|装|用|够了|不需要)|电脑.*管家.*要你", text):
        return "用竞品"
    if any(kw in text for kw in rival_kws):
        return "用竞品"
    # 新增：已有相关/类型相同/杀毒太多
    if re.search(r"(已有|有|装了|用了).{0,6}(相关|类似|相同|类型).{0,4}(软件|工具|杀毒)", text):
        return "用竞品"
    if re.search(r"杀毒软件.{0,4}(太多|重复|重叠)", text):
        return "用竞品"

    # 不想用/不用了
    if re.search(r"不想用|不用了|不想再用|不想继续用|用不下去", text):
        return "不好用、不想用、不需要"
    # 不需要
    if any(kw in text for kw in no_need_kws) or re.search(r"(不想要|不需要|不想装|不想安装|不要了)", text):
        return "不好用、不想用、不需要"

    if any(kw in text for kw in func_kws) or re.search(r"(第三方软件|软件).{0,8}(读取|监控|访问)", text):
        return "功能问题"
    # "无法登录会员/会员登录" → 功能问题（先于收费判断）
    if re.search(r"(无法|不能|登录不了).{0,4}(登录|会员|账号)", text):
        return "功能问题"
    # "程序错误/无法使用/不能用" → 功能问题
    if re.search(r"(程序|软件|系统).{0,4}(错误|异常|出问题|出错|无法使用|不能用)", text):
        return "功能问题"
    if any(kw in text for kw in bad_kws):
        return "不好用、不想用、不需要"

    # 补充细化规则（常见边界情况）
    # 影响开机/影响系统 → 软件兼容问题
    if re.search(r"(影响|导致).{0,6}(开机|系统|启动|引导)", text):
        return "软件兼容问题"
    # 软件重复/已有类似
    if re.search(r"(软件|功能).{0,4}(重复|类似|一样|雷同)", text):
        return "不好用、不想用、不需要"
    # 看到就烦/很烦/烦死了
    if re.search(r"(就|太|很|好|极其)?(烦|厌烦|讨厌|烦透了|烦死)", text):
        return "不好用、不想用、不需要"
    # 你姥姥/你大爷 等委婉骂人（仅在无具体诉求时归谩骂）
    if re.search(r"(你姥|你大爷|你爸|你儿子|去你|你们.*死|见鬼)", text) and not has_specific:
        return "谩骂"
    # 兜底谩骂：含骂人词但未匹配具体分类
    if any(kw in text for kw in abuse_kws):
        return "谩骂"
    # "垃圾"/"laji"/"太垃圾了" 等短纯骂句 → 谩骂
    if re.search(r"^(垃圾|很垃圾|太垃圾了?|超级垃圾|真垃圾|好垃圾|垃圾软件|一个垃圾|垃圾1|雷子垃圾|就是垃圾|真是垃圾|完全垃圾)[!！。.，,\s]*$", text.strip()):
        return "谩骂"
    if re.search(r"^(laji|la ji|lajishijian|lj)[!!\s]*$", text.strip().lower()):
        return "谩骂"

    # ── 学习规则兜底：匹配 learned_rules 中的动态关键词 ──────────────────────
    # 遍历各分类的学习关键词，取命中长词最长的分类（优先长词避免误匹配）
    _learned_cat_match = None
    _learned_match_len = 0
    for _cat, _kws in _learned_add_keywords.items():
        if _cat in ("其他", "其他原因"):
            continue
        # 跳过该分类有排除词命中的情况
        _remove_kws = _learned_remove_keywords.get(_cat, [])
        if any(_rkw in text or _rkw in text.lower() for _rkw in _remove_kws):
            continue
        for _kw in sorted(_kws, key=len, reverse=True):  # 长词优先
            if _kw in text or _kw in text.lower():
                if len(_kw) > _learned_match_len:
                    _learned_cat_match = _cat
                    _learned_match_len = len(_kw)
                break
    if _learned_cat_match:
        return _learned_cat_match

    return "其他"


def classify_feedback(text: str) -> str:
    if not text or not text.strip():
        return '其他'
    text = text.strip()
    cache_key = hashlib.md5(text.encode('utf-8')).hexdigest()
    if cache_key in _classify_cache:
        return _classify_cache[cache_key]

    api_key = os.environ.get('OPENAI_API_KEY', '')
    api_base = os.environ.get('OPENAI_API_BASE', 'https://api.openai.com/v1')
    if api_key:
        try:
            from openai import OpenAI
            client = OpenAI(api_key=api_key, base_url=api_base)
            cats = '\n'.join(f"- {c}" for c in CATEGORIES)
            prompt = f"你是产品反馈分类助手。将用户卸载反馈归入以下分类之一，只输出分类名，不要其他内容。\n\n分类：\n{cats}\n\n反馈：{text}\n\n分类："
            resp = client.chat.completions.create(
                model='gpt-3.5-turbo',
                messages=[{'role': 'user', 'content': prompt}],
                max_tokens=30, temperature=0
            )
            result = resp.choices[0].message.content.strip()
            if result in CATEGORIES:
                _classify_cache[cache_key] = result
                save_cache()
                return result
        except Exception as e:
            print(f'OpenAI error: {e}')

    result = rule_classify(text)
    _classify_cache[cache_key] = result
    save_cache()
    return result


def get_db():
    return pymysql.connect(**DB_CONFIG)


def serialize_row(row: dict) -> dict:
    for k, v in row.items():
        if isinstance(v, date):
            row[k] = str(v)
    return row


@app.route('/')
def index():
    return render_template_string(HTML_TEMPLATE)


@app.route('/api/dates')
def get_dates():
    try:
        conn = get_db()
        with conn.cursor() as cur:
            cur.execute("SELECT DISTINCT date FROM duba_ball_feedback_new ORDER BY date DESC LIMIT 60")
            dates = [str(r['date']) for r in cur.fetchall()]
        conn.close()
        return jsonify({'dates': dates})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


def _query_feedback_from_db(date_start: str, date_end: str) -> dict:
    """从数据库查询指定日期范围的反馈，返回原始行（不含分类）"""
    conn = get_db()
    with conn.cursor() as cur:
        cur.execute("""
            SELECT
                id, date, tid1, tid2, tod1, tod2,
                svrid,
                tryno,
                days,
                qq AS qqnum,
                tel,
                wechat,
                vip_type AS isvip,
                time,
                sys AS os,
                feedback AS word,
                v_from,
                from_ AS src_from
            FROM duba_ball_feedback_new
            WHERE date BETWEEN %s AND %s
            ORDER BY date DESC, time DESC
        """, (date_start, date_end))
        rows = cur.fetchall()
    conn.close()

    all_rows = []
    for row in rows:
        serialize_row(row)
        all_rows.append(row)

    return {
        'date_start': date_start,
        'date_end': date_end,
        'data': all_rows  # 全量原始行，含无效数据，无 category 字段
    }


def _apply_filter_and_classify(rows: list) -> tuple:
    """对全量原始行实时过滤无效 + 打分类标签，返回 (valid_rows, invalid_count)"""
    valid_rows = []
    invalid_count = 0
    for row in rows:
        word = (row.get('word') or '').strip()
        if is_valid_feedback(word):
            row['category'] = classify_feedback(word)
            valid_rows.append(row)
        else:
            invalid_count += 1
    return valid_rows, invalid_count


@app.route('/api/feedback')
def get_feedback():
    """
    支持单日期（date=）或日期范围（date_start= & date_end=）
    历史日期（非当天）的查询结果缓存到本地文件，避免重复查库。
    当天数据每次实时查询（当天数据可能不完整）。
    """
    date_start = request.args.get('date_start', '')
    date_end = request.args.get('date_end', '')
    single_date = request.args.get('date', '')

    yesterday = str(date.today() - timedelta(days=1))
    today_str = str(date.today())

    if single_date:
        date_start = date_end = single_date
    elif not date_start and not date_end:
        date_start = date_end = yesterday

    if not date_start:
        date_start = date_end
    if not date_end:
        date_end = date_start

    try:
        # 生成日期列表
        from datetime import datetime
        d_start = datetime.strptime(date_start, '%Y-%m-%d').date()
        d_end = datetime.strptime(date_end, '%Y-%m-%d').date()
        all_dates = []
        d = d_start
        while d <= d_end:
            all_dates.append(str(d))
            d += timedelta(days=1)

        # 分成"当天"和"历史"两类
        today_dates = [d for d in all_dates if d == today_str]
        history_dates = [d for d in all_dates if d != today_str]

        # 历史日期：有缓存直接用，没缓存才查库
        cached_dates = []
        uncached_dates = []
        for d in history_dates:
            if _load_feedback_cache(d) is not None:
                cached_dates.append(d)
            else:
                uncached_dates.append(d)

        all_valid_rows = []
        total_invalid = 0

        # 读取历史缓存（全量原始数据），实时过滤 + 实时分类
        for d in cached_dates:
            cached = _load_feedback_cache(d)
            valid_rows, inv_count = _apply_filter_and_classify(list(cached['data']))
            all_valid_rows.extend(valid_rows)
            total_invalid += inv_count

        # 查库：未缓存的历史日期 + 当天
        db_dates = uncached_dates + today_dates
        if db_dates:
            db_start = min(db_dates)
            db_end = max(db_dates)
            result = _query_feedback_from_db(db_start, db_end)

            # 按日期拆分，历史日期存缓存（全量原始数据，不含 category/过滤）
            by_date = {}
            for row in result['data']:
                rd = row.get('date', '')
                by_date.setdefault(rd, []).append(row)

            for d in uncached_dates:
                rows_for_date = by_date.get(d, [])
                cache_payload = {
                    'date_start': d,
                    'date_end': d,
                    'data': rows_for_date  # 全量原始数据
                }
                _save_feedback_cache(d, cache_payload)

            # 实时过滤 + 实时分类后合并
            valid_rows, inv_count = _apply_filter_and_classify(result['data'])
            all_valid_rows.extend(valid_rows)
            total_invalid += inv_count

        # 按 date DESC, time DESC 排序
        all_valid_rows.sort(key=lambda r: (r.get('date', ''), r.get('time', '') or ''), reverse=True)

        return jsonify({
            'date_start': date_start,
            'date_end': date_end,
            'total': len(all_valid_rows),
            'invalid_count': total_invalid,
            'cached_dates': cached_dates,
            'data': all_valid_rows
        })
    except Exception as e:
        import traceback
        return jsonify({'error': str(e), 'trace': traceback.format_exc()}), 500


def _write_invalid_log(word: str, action: str, reason: str = ''):
    import datetime
    entry = {
        'ts': datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        'action': action,
        'word': word,
        'reason': reason,
    }
    try:
        with open(INVALID_LOG, 'a', encoding='utf-8') as f:
            f.write(json.dumps(entry, ensure_ascii=False) + '\n')
    except Exception as e:
        print(f'[invalid_log] write error: {e}')


@app.route('/api/mark_invalid', methods=['POST'])
def mark_invalid():
    """手动标记某条反馈为无效，写日志并加入内存黑名单"""
    data = request.get_json(force=True)
    word = (data.get('word') or '').strip()
    reason = (data.get('reason') or '').strip()
    if not word:
        return jsonify({'error': 'word is required'}), 400
    already = word in _manual_invalid
    _manual_invalid.add(word)
    _write_invalid_log(word, 'mark_invalid', reason)
    return jsonify({'ok': True, 'word': word, 'already': already})


@app.route('/api/mark_valid', methods=['POST'])
def mark_valid():
    """撤销手动无效标记"""
    data = request.get_json(force=True)
    word = (data.get('word') or '').strip()
    if not word:
        return jsonify({'error': 'word is required'}), 400
    was_invalid = word in _manual_invalid
    _manual_invalid.discard(word)
    _write_invalid_log(word, 'mark_valid', '')
    return jsonify({'ok': True, 'word': word, 'was_invalid': was_invalid})


@app.route('/api/invalid_log')
def get_invalid_log():
    """返回手动无效标记日志"""
    limit = int(request.args.get('limit', 200))
    if not os.path.exists(INVALID_LOG):
        return jsonify({'total': 0, 'blacklist_size': 0, 'data': []})
    entries = []
    with open(INVALID_LOG, 'r', encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entries.append(json.loads(line))
            except:
                pass
    entries.reverse()
    return jsonify({
        'total': len(entries),
        'blacklist_size': len(_manual_invalid),
        'data': entries[:limit]
    })


@app.route('/api/reclassify', methods=['POST'])
def reclassify():
    """手动修改某条反馈的分类，写入缓存并记录纠正日志"""
    data = request.get_json(force=True)
    word = (data.get('word') or '').strip()
    category = (data.get('category') or '').strip()
    if not word:
        return jsonify({'error': 'word is required'}), 400
    if category not in CATEGORIES:
        return jsonify({'error': f'invalid category: {category}'}), 400
    cache_key = hashlib.md5(word.encode('utf-8')).hexdigest()
    old_category = _classify_cache.get(cache_key) or rule_classify(word)
    _classify_cache[cache_key] = category
    save_cache()
    # 只在分类真正改变时写日志
    if old_category != category:
        log_correction(word, old_category, category)
    return jsonify({'ok': True, 'word': word, 'old': old_category, 'category': category})


@app.route('/api/corrections')
def get_corrections():
    """返回纠正日志，支持按 old/new 分类筛选，默认返回最近100条"""
    limit = int(request.args.get('limit', 100))
    filter_old = request.args.get('old', '')
    filter_new = request.args.get('new', '')
    if not os.path.exists(CORRECTION_LOG):
        return jsonify({'total': 0, 'data': []})
    entries = []
    with open(CORRECTION_LOG, 'r', encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                e = json.loads(line)
                if filter_old and e.get('old') != filter_old:
                    continue
                if filter_new and e.get('new') != filter_new:
                    continue
                entries.append(e)
            except:
                pass
    entries.reverse()  # 最新在前
    return jsonify({'total': len(entries), 'data': entries[:limit]})


@app.route('/api/trend')
def get_trend():
    """按日期分组，返回各分类每天的反馈量，用于趋势折线图"""
    date_start = request.args.get('date_start', '')
    date_end = request.args.get('date_end', '')
    yesterday = str(date.today() - timedelta(days=1))
    week_ago = str(date.today() - timedelta(days=7))

    if not date_start and not date_end:
        date_start = week_ago
        date_end = yesterday
    if not date_start:
        date_start = date_end
    if not date_end:
        date_end = date_start

    try:
        conn = get_db()
        with conn.cursor() as cur:
            cur.execute("""
                SELECT date, feedback AS word FROM duba_ball_feedback_new
                WHERE date BETWEEN %s AND %s
                ORDER BY date ASC
            """, (date_start, date_end))
            rows = cur.fetchall()
        conn.close()

        # date -> category -> count
        from collections import defaultdict
        date_cat_count = defaultdict(lambda: defaultdict(int))
        all_dates = sorted(set(str(r['date']) for r in rows))

        for row in rows:
            word = (row.get('word') or '').strip()
            if is_valid_feedback(word):
                cat = classify_feedback(word)
                date_cat_count[str(row['date'])][cat] += 1

        # 整理成 {category: [counts by date]}
        all_cats = set()
        for dc in date_cat_count.values():
            all_cats.update(dc.keys())
        all_cats = sorted(all_cats)

        series = {}
        for cat in all_cats:
            series[cat] = [date_cat_count[d].get(cat, 0) for d in all_dates]

        return jsonify({
            'date_start': date_start,
            'date_end': date_end,
            'dates': all_dates,
            'series': series
        })
    except Exception as e:
        import traceback
        return jsonify({'error': str(e), 'trace': traceback.format_exc()}), 500


@app.route('/api/stats')
def get_stats():
    date_start = request.args.get('date_start', '')
    date_end = request.args.get('date_end', '')
    single_date = request.args.get('date', '')
    yesterday = str(date.today() - timedelta(days=1))

    if single_date:
        date_start = date_end = single_date
    elif not date_start and not date_end:
        date_start = date_end = yesterday
    if not date_start:
        date_start = date_end
    if not date_end:
        date_end = date_start

    try:
        conn = get_db()
        with conn.cursor() as cur:
            cur.execute("""
                SELECT feedback AS word FROM duba_ball_feedback_new
                WHERE date BETWEEN %s AND %s
                AND tryno IN (1509, 1517, 1335)
            """, (date_start, date_end))
            rows = cur.fetchall()
        conn.close()

        stats = {}
        for row in rows:
            word = (row.get('word') or '').strip()
            if is_valid_feedback(word):
                cat = classify_feedback(word)
                stats[cat] = stats.get(cat, 0) + 1

        stats_list = sorted([{'category': k, 'count': v} for k, v in stats.items()], key=lambda x: -x['count'])
        return jsonify({'date_start': date_start, 'date_end': date_end, 'stats': stats_list})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


# ─────────────────────────────────────────────
# HTML 模板
# ─────────────────────────────────────────────
HTML_TEMPLATE = r'''<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>毒霸用户后台反馈分类</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif;background:#f0f2f5;color:#333;font-size:13px}

/* ── Header ── */
.header{background:linear-gradient(135deg,#1a1a2e,#16213e,#0f3460);color:#fff;padding:14px 24px;display:flex;align-items:center;gap:12px;box-shadow:0 2px 8px rgba(0,0,0,.25)}
.header h1{font-size:18px;font-weight:600}
.header .sub{font-size:12px;opacity:.75;margin-top:2px}

/* ── Toolbar ── */
.toolbar{background:#fff;padding:10px 24px;display:flex;align-items:center;gap:10px;flex-wrap:wrap;border-bottom:1px solid #e8e8e8}
.toolbar label{color:#555;white-space:nowrap}
.toolbar input[type=date],.toolbar select,.toolbar input[type=text]{border:1px solid #d9d9d9;border-radius:4px;padding:5px 8px;font-size:13px;outline:none;color:#333}
.toolbar input[type=date]:focus,.toolbar select:focus,.toolbar input[type=text]:focus{border-color:#0f3460}
.sep{color:#ccc;margin:0 2px}
.btn{padding:5px 14px;border-radius:4px;border:none;cursor:pointer;font-size:13px;font-weight:500;transition:background .15s}
.btn-primary{background:#0f3460;color:#fff}.btn-primary:hover{background:#0c2d6b}
.btn-export{background:#0f3460;color:#fff}.btn-export:hover{background:#0c2d6b}
.search-input{width:180px}

/* ── Stats chips ── */
.stats-bar{background:#fff;padding:8px 24px;border-bottom:1px solid #e8e8e8;display:flex;gap:8px;flex-wrap:wrap;align-items:center}
.chip{display:inline-flex;align-items:center;gap:4px;padding:3px 10px;border-radius:12px;background:#e8eaf6;color:#0c2d6b;cursor:pointer;transition:all .15s;white-space:nowrap;border:1px solid transparent}
.chip:hover{border-color:#0f3460}
.chip.active{background:#0f3460;color:#fff}
.chip .cnt{font-weight:700}

/* ── Filter bar (category dropdown) ── */
.filter-bar{background:#fafafa;padding:8px 24px;border-bottom:1px solid #ebebeb;display:flex;align-items:center;gap:10px;flex-wrap:wrap}
.filter-bar label{color:#555}

/* ── Content ── */
.content{padding:14px 24px}
.info-row{display:flex;justify-content:space-between;align-items:center;margin-bottom:10px;flex-wrap:wrap;gap:6px}
.info-text{color:#888;font-size:12px}
.info-text strong{color:#1a237e;font-weight:600}

/* ── Table ── */
.table-wrap{background:#fff;border-radius:8px;box-shadow:0 1px 4px rgba(0,0,0,.08);overflow:auto}
table{width:100%;border-collapse:collapse;min-width:1300px}
thead th{background:#e8eaf6;color:#0c2d6b;padding:9px 10px;text-align:left;white-space:nowrap;position:sticky;top:0;z-index:1;font-weight:600;border-bottom:2px solid #c5cae9}
tbody tr{border-bottom:1px solid #f0f0f0;transition:background .1s}
tbody tr:hover{background:#f5f5ff}
td{padding:7px 10px;vertical-align:middle;max-width:180px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
td.word-col{max-width:280px;white-space:normal;word-break:break-all;line-height:1.5}

/* ── Tags ── */
.tag-vip{display:inline-block;padding:1px 7px;border-radius:10px;background:#fff3e0;color:#e65100;font-size:11px}
.tag-normal{display:inline-block;padding:1px 7px;border-radius:10px;background:#e8eaf6;color:#999;font-size:11px}
.cat-tag{display:inline-block;padding:2px 8px;border-radius:4px;background:#e8eaf6;color:#0c2d6b;font-size:11px;white-space:nowrap}
/* ── 手动分类下拉 ── */
.cat-select{border:1px solid #c5cae9;border-radius:4px;background:#e8eaf6;color:#0c2d6b;font-size:11px;padding:2px 4px;cursor:pointer;outline:none;max-width:160px}
.cat-select:focus{border-color:#0f3460;background:#fff}
.cat-select.saving{opacity:.5;pointer-events:none}
.cat-select.saved{border-color:#43a047;background:#e8f5e9;color:#2e7d32}
.save-tip{font-size:10px;color:#43a047;margin-left:4px;display:none}
.save-tip.show{display:inline}
/* ── 手动无效标记 ── */
.btn-invalid{background:none;border:none;cursor:pointer;font-size:13px;padding:1px 4px;border-radius:3px;opacity:.4;transition:opacity .15s;vertical-align:middle;line-height:1}
.btn-invalid:hover{opacity:1;background:#fff0f0}
.btn-invalid.marked{opacity:1;color:#e53935}
tr.row-invalid{opacity:.35;background:#fafafa!important}
tr.row-invalid td{text-decoration:line-through;color:#bbb}

/* ── Pagination ── */
.pag{display:flex;align-items:center;gap:6px;margin-top:14px;justify-content:center;flex-wrap:wrap}
.pag button{padding:4px 10px;border:1px solid #d9d9d9;border-radius:4px;background:#fff;cursor:pointer;font-size:12px}
.pag button:hover{border-color:#0f3460;color:#1a237e}
.pag button.active{background:#0f3460;color:#fff;border-color:#0f3460}
.pag .pinfo{font-size:12px;color:#999;margin-left:6px}

.loading{text-align:center;padding:50px;color:#aaa}
.empty{text-align:center;padding:50px;color:#bbb}

/* ── Tabs ── */
.tabs{display:flex;gap:0;background:#fff;border-bottom:2px solid #e8eaf6;padding:0 24px}
.tab-btn{padding:10px 20px;cursor:pointer;font-size:13px;font-weight:500;color:#888;border-bottom:2px solid transparent;margin-bottom:-2px;transition:all .15s;background:none;border-top:none;border-left:none;border-right:none}
.tab-btn:hover{color:#1a237e}
.tab-btn.active{color:#1a237e;border-bottom:2px solid #1a237e;font-weight:600}
.tab-panel{display:none}.tab-panel.active{display:block}

/* ── 趋势图 / 占比图 共用 ── */
.trend-wrap{background:#fff;border-radius:8px;box-shadow:0 1px 4px rgba(0,0,0,.08);padding:20px;margin-top:14px}
.trend-legend{display:flex;flex-wrap:wrap;gap:8px;margin-bottom:16px}
.legend-item{display:inline-flex;align-items:center;gap:5px;padding:3px 10px;border-radius:12px;cursor:pointer;font-size:12px;border:1px solid transparent;transition:all .15s;user-select:none}
.legend-item:hover{border-color:currentColor}
.legend-item.dimmed{opacity:.25}
.legend-dot{width:10px;height:10px;border-radius:50%;flex-shrink:0}
.trend-canvas-wrap{position:relative;height:420px}
.trend-empty{text-align:center;padding:80px;color:#bbb}
/* 占比图 tip */
.share-tip{font-size:12px;color:#888;margin-left:4px}
</style>
</head>
<body>

<div class="header">
  <div>
    <h1>🛡️ 毒霸用户后台反馈分类</h1>
    <div class="sub">展示有效的用户反馈内容 · AI自动分类</div>
  </div>
</div>

<!-- 工具栏 -->
<div class="toolbar">
  <label>日期范围：</label>
  <input type="date" id="dateStart">
  <span class="sep">至</span>
  <input type="date" id="dateEnd">
  <button class="btn btn-primary" onclick="onQuery()">🔍 查询</button>
  <button class="btn" onclick="onQuery()" style="background:#f0f0f0">🔄 刷新</button>
  <button class="btn" onclick="setQuickDate('yesterday')" style="background:#f0f0f0">昨天</button>
  <button class="btn" onclick="setQuickDate('week')" style="background:#f0f0f0">近7天</button>
  <button class="btn" onclick="setQuickDate('month')" style="background:#f0f0f0">近30天</button>
  <label style="margin-left:8px">每页：</label>
  <select id="pageSizeSelect" onchange="onPageSizeChange()">
    <option value="50">50条</option>
    <option value="100">100条</option>
    <option value="200">200条</option>
    <option value="9999">全部</option>
  </select>
  <input class="search-input" type="text" id="searchBox" placeholder="搜索反馈内容/UUID..." oninput="onSearch()">
  <button class="btn btn-export" onclick="exportCSV()">📥 导出CSV</button>
</div>

<!-- 标签页切换 -->
<div class="tabs">
  <button class="tab-btn active" onclick="switchTab('table')">📋 反馈列表</button>
  <button class="tab-btn" onclick="switchTab('trend')">📈 分类趋势</button>
  <button class="tab-btn" onclick="switchTab('share')">🥧 分类占比趋势</button>
</div>

<!-- 分类筛选下拉 + 分类统计chips -->
<!-- 反馈列表面板 -->
<div id="panel-table" class="tab-panel active">
  <div class="filter-bar">
    <label>分类筛选：</label>
    <select id="catSelect" onchange="onCatSelect()">
      <option value="">全部分类</option>
    </select>
  </div>

  <div class="stats-bar" id="statsBar">
    <span style="color:#aaa">请先选择日期查询</span>
  </div>

  <div class="content">
    <div class="info-row">
      <div class="info-text" id="infoDesc">—</div>
    </div>
    <div class="table-wrap">
      <table>
        <thead>
          <tr>
            <th>日期</th>
            <th>from_</th>
            <th>v_from</th>
            <th>tryno</th>
            <th>tid1</th><th>tid2</th><th>tod1</th><th>tod2</th>
            <th>安装天数</th>
            <th>svrid信息</th>
            <th class="word-col">用户反馈内容</th>
            <th>QQ</th><th>微信</th><th>电话</th>
            <th>是否会员</th>
            <th>反馈时间</th>
            <th>系统</th>
            <th>分类</th>
            <th>操作</th>
          </tr>
        </thead>
        <tbody id="tableBody">
          <tr><td colspan="18" class="loading">请选择日期后点击查询</td></tr>
        </tbody>
      </table>
    </div>
    <div class="pag" id="pagination"></div>
  </div>
</div>

<!-- 趋势图面板 -->
<div id="panel-trend" class="tab-panel">
  <div class="content">
    <div class="trend-wrap">
      <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:12px;flex-wrap:wrap;gap:8px">
        <div style="font-size:13px;color:#555">
          各分类弹泡反馈量趋势（点击图例可显示/隐藏）
        </div>
        <div style="display:flex;gap:8px">
          <button class="btn" onclick="trendToggleAll(true)" style="background:#f0f0f0;font-size:12px;padding:4px 10px">全显示</button>
          <button class="btn" onclick="trendToggleAll(false)" style="background:#f0f0f0;font-size:12px;padding:4px 10px">全隐藏</button>
        </div>
      </div>
      <div class="trend-legend" id="trendLegend"></div>
      <div class="trend-canvas-wrap">
        <canvas id="trendChart"></canvas>
        <div class="trend-empty" id="trendEmpty" style="display:none">暂无数据，请先查询</div>
      </div>
    </div>
  </div>
</div>

<!-- 占比趋势面板 -->
<div id="panel-share" class="tab-panel">
  <div class="content">
    <div class="trend-wrap">
      <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:12px;flex-wrap:wrap;gap:8px">
        <div style="font-size:13px;color:#555">
          各分类占比趋势（堆叠面积图，悬浮查看每日百分比）<span class="share-tip">· 点击图例可显示/隐藏</span>
        </div>
        <div style="display:flex;gap:8px">
          <button class="btn" onclick="shareToggleAll(true)" style="background:#f0f0f0;font-size:12px;padding:4px 10px">全显示</button>
          <button class="btn" onclick="shareToggleAll(false)" style="background:#f0f0f0;font-size:12px;padding:4px 10px">全隐藏</button>
        </div>
      </div>
      <div class="trend-legend" id="shareLegend"></div>
      <div class="trend-canvas-wrap">
        <canvas id="shareChart"></canvas>
        <div class="trend-empty" id="shareEmpty" style="display:none">暂无数据，请先查询</div>
      </div>
    </div>
  </div>
</div>

<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
<script>
const OS_MAP = {'10':'WinXP','11':'Win7','12':'Win8','19':'Win8.1','20':'Win10','21':'Win11','22':'Win12'};
const CATEGORIES = [
  "被捆绑安装，反复安装","不好用、不想用、不需要","弹窗广告","功能问题",
  "其他","其他原因","软件兼容问题","收费原因","推广安装其他软件、插件",
  "误删误拦","修改默认","影响其他软件","用竞品","占用高，导致电脑卡慢","谩骂"
];

// 分类配色（固定，保证每次颜色一致）
const CAT_COLORS = {
  "被捆绑安装，反复安装": "#e53935",
  "不好用、不想用、不需要": "#8e24aa",
  "弹窗广告": "#f4511e",
  "功能问题": "#818cf8",
  "其他": "#a78bfa",
  "其他原因": "#b0bec5",
  "软件兼容问题": "#3949ab",
  "收费原因": "#f9a825",
  "推广安装其他软件、插件": "#d81b60",
  "误删误拦": "#6d4c41",
  "修改默认": "#5c6bc0",
  "影响其他软件": "#43a047",
  "用竞品": "#7986cb",
  "占用高，导致电脑卡慢": "#fb8c00",
  "谩骂": "#757575"
};
function getCatColor(cat) { return CAT_COLORS[cat] || '#9e9e9e'; }

let allData = [];       // 接口返回全量
let filteredData = [];  // 本地筛选后
let currentPage = 1;
let pageSize = 50;
let activeCategory = '';
let searchText = '';
let currentTab = 'table';

// ── 标签页切换 ──
const TAB_IDS = ['table', 'trend', 'share'];
function switchTab(tab) {
  currentTab = tab;
  document.querySelectorAll('.tab-btn').forEach((b, i) => {
    b.classList.toggle('active', TAB_IDS[i] === tab);
  });
  TAB_IDS.forEach(id => {
    document.getElementById('panel-' + id).classList.toggle('active', id === tab);
  });
  if (tab === 'trend' || tab === 'share') {
    // 若当前是单天（start===end），自动扩展为近7天
    const ds = document.getElementById('dateStart').value;
    const de = document.getElementById('dateEnd').value;
    if (ds && de && ds === de) {
      document.getElementById('dateStart').value = getOffsetDate(-7);
      document.getElementById('dateEnd').value = getOffsetDate(-1);
    }
    if (tab === 'trend') loadTrend();
    if (tab === 'share') loadShare();
  }
}

// ── 趋势图 ──
let trendChart = null;
let trendHidden = {};  // cat -> bool

async function loadTrend() {
  const ds = document.getElementById('dateStart').value;
  const de = document.getElementById('dateEnd').value;
  if (!ds || !de) return;

  document.getElementById('trendEmpty').style.display = 'none';
  document.getElementById('trendLegend').innerHTML = '<span style="color:#aaa">加载中...</span>';

  try {
    const res = await fetch(`/api/trend?date_start=${ds}&date_end=${de}`);
    const json = await res.json();
    if (json.error) throw new Error(json.error);

    const { dates, series } = json;
    if (!dates || dates.length === 0) {
      document.getElementById('trendEmpty').style.display = 'block';
      document.getElementById('trendLegend').innerHTML = '';
      if (trendChart) { trendChart.destroy(); trendChart = null; }
      return;
    }

    // 按总量降序排列分类
    const cats = Object.keys(series).sort((a,b) => {
      const sumA = series[a].reduce((s,v)=>s+v,0);
      const sumB = series[b].reduce((s,v)=>s+v,0);
      return sumB - sumA;
    });

    // 渲染图例
    renderTrendLegend(cats);

    // 构建 datasets
    const datasets = cats.map(cat => ({
      label: cat,
      data: series[cat],
      borderColor: getCatColor(cat),
      backgroundColor: getCatColor(cat) + '22',
      borderWidth: 2,
      pointRadius: dates.length <= 14 ? 4 : 2,
      pointHoverRadius: 6,
      tension: 0.3,
      fill: false,
      hidden: trendHidden[cat] || false,
    }));

    // 销毁旧图
    if (trendChart) { trendChart.destroy(); trendChart = null; }

    const ctx = document.getElementById('trendChart').getContext('2d');
    trendChart = new Chart(ctx, {
      type: 'line',
      data: { labels: dates, datasets },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        interaction: { mode: 'index', intersect: false },
        plugins: {
          legend: { display: false },
          tooltip: {
            callbacks: {
              title: items => '日期：' + items[0].label,
              label: item => `  ${item.dataset.label}：${item.parsed.y} 条`
            },
            itemSort: (a,b) => b.parsed.y - a.parsed.y
          }
        },
        scales: {
          x: {
            ticks: { maxRotation: 45, font: { size: 11 } },
            grid: { color: '#f0f0f0' }
          },
          y: {
            beginAtZero: true,
            ticks: { stepSize: 1, font: { size: 11 } },
            grid: { color: '#f0f0f0' }
          }
        }
      }
    });

    // 同步 hidden 状态
    cats.forEach((cat, i) => {
      if (trendHidden[cat]) trendChart.hide(i);
    });

  } catch(e) {
    document.getElementById('trendLegend').innerHTML = `<span style="color:red">加载失败：${e.message}</span>`;
  }
}

function renderTrendLegend(cats) {
  const legend = document.getElementById('trendLegend');
  legend.innerHTML = '';
  cats.forEach(cat => {
    const color = getCatColor(cat);
    const dimmed = trendHidden[cat] ? ' dimmed' : '';
    const item = document.createElement('div');
    item.className = 'legend-item' + dimmed;
    item.style.color = color;
    item.innerHTML = `<span class="legend-dot" style="background:${color}"></span>${cat}`;
    item.onclick = () => toggleTrendCat(cat, cats);
    legend.appendChild(item);
  });
}

function toggleTrendCat(cat, cats) {
  trendHidden[cat] = !trendHidden[cat];
  renderTrendLegend(cats);
  if (!trendChart) return;
  const idx = trendChart.data.datasets.findIndex(d => d.label === cat);
  if (idx >= 0) {
    if (trendHidden[cat]) trendChart.hide(idx);
    else trendChart.show(idx);
  }
}

function trendToggleAll(show) {
  if (!trendChart) return;
  trendChart.data.datasets.forEach((ds, i) => {
    trendHidden[ds.label] = !show;
    if (show) trendChart.show(i);
    else trendChart.hide(i);
  });
  const cats = trendChart.data.datasets.map(d=>d.label);
  renderTrendLegend(cats);
}

// ── 占比趋势图 ──
let shareChart = null;
let shareHidden = {};  // cat -> bool
let _shareCacheKey = '';
let _shareData = null;

async function loadShare() {
  const ds = document.getElementById('dateStart').value;
  const de = document.getElementById('dateEnd').value;
  if (!ds || !de) return;

  document.getElementById('shareEmpty').style.display = 'none';
  document.getElementById('shareLegend').innerHTML = '<span style="color:#aaa">加载中...</span>';

  // 复用 trend 接口数据（同 key 不重复请求）
  const cacheKey = ds + '_' + de;
  try {
    let trendJson = _shareData;
    if (_shareCacheKey !== cacheKey || !trendJson) {
      const res = await fetch(`/api/trend?date_start=${ds}&date_end=${de}`);
      trendJson = await res.json();
      if (trendJson.error) throw new Error(trendJson.error);
      _shareData = trendJson;
      _shareCacheKey = cacheKey;
    }

    const { dates, series } = trendJson;
    if (!dates || dates.length === 0) {
      document.getElementById('shareEmpty').style.display = 'block';
      document.getElementById('shareLegend').innerHTML = '';
      if (shareChart) { shareChart.destroy(); shareChart = null; }
      return;
    }

    // 按总量降序
    const cats = Object.keys(series).sort((a,b) => {
      const sumA = series[a].reduce((s,v)=>s+v,0);
      const sumB = series[b].reduce((s,v)=>s+v,0);
      return sumB - sumA;
    });

    // 计算每天总量，转成百分比（保留1位小数）
    const dailyTotals = dates.map((_, di) =>
      cats.reduce((s, cat) => s + (series[cat][di] || 0), 0)
    );
    const pctSeries = {};
    cats.forEach(cat => {
      pctSeries[cat] = dates.map((_, di) => {
        const total = dailyTotals[di];
        return total > 0 ? parseFloat(((series[cat][di] || 0) / total * 100).toFixed(1)) : 0;
      });
    });

    renderShareLegend(cats);

    const datasets = cats.map(cat => ({
      label: cat,
      data: pctSeries[cat],
      borderColor: getCatColor(cat),
      backgroundColor: getCatColor(cat) + 'bb',
      borderWidth: 1,
      pointRadius: 0,
      pointHoverRadius: 5,
      tension: 0.3,
      fill: true,
      hidden: shareHidden[cat] || false,
    }));

    if (shareChart) { shareChart.destroy(); shareChart = null; }

    const ctx = document.getElementById('shareChart').getContext('2d');
    shareChart = new Chart(ctx, {
      type: 'line',
      data: { labels: dates, datasets },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        interaction: { mode: 'index', intersect: false },
        plugins: {
          legend: { display: false },
          tooltip: {
            callbacks: {
              title: items => '日期：' + items[0].label,
              label: item => {
                const di = item.dataIndex;
                const rawCnt = series[item.dataset.label][di] || 0;
                return `  ${item.dataset.label}：${item.parsed.y}%（${rawCnt} 条）`;
              }
            },
            itemSort: (a,b) => b.parsed.y - a.parsed.y
          }
        },
        scales: {
          x: {
            ticks: { maxRotation: 45, font: { size: 11 } },
            grid: { color: '#f0f0f0' }
          },
          y: {
            stacked: true,
            min: 0,
            max: 100,
            ticks: {
              callback: v => v + '%',
              font: { size: 11 }
            },
            grid: { color: '#f0f0f0' }
          }
        }
      }
    });

    cats.forEach((cat, i) => {
      if (shareHidden[cat]) shareChart.hide(i);
    });

  } catch(e) {
    document.getElementById('shareLegend').innerHTML = `<span style="color:red">加载失败：${e.message}</span>`;
  }
}

function renderShareLegend(cats) {
  const legend = document.getElementById('shareLegend');
  legend.innerHTML = '';
  cats.forEach(cat => {
    const color = getCatColor(cat);
    const dimmed = shareHidden[cat] ? ' dimmed' : '';
    const item = document.createElement('div');
    item.className = 'legend-item' + dimmed;
    item.style.color = color;
    item.innerHTML = `<span class="legend-dot" style="background:${color}"></span>${cat}`;
    item.onclick = () => toggleShareCat(cat, cats);
    legend.appendChild(item);
  });
}

function toggleShareCat(cat, cats) {
  shareHidden[cat] = !shareHidden[cat];
  renderShareLegend(cats);
  if (!shareChart) return;
  const idx = shareChart.data.datasets.findIndex(d => d.label === cat);
  if (idx >= 0) {
    if (shareHidden[cat]) shareChart.hide(idx);
    else shareChart.show(idx);
  }
}

function shareToggleAll(show) {
  if (!shareChart) return;
  shareChart.data.datasets.forEach((ds, i) => {
    shareHidden[ds.label] = !show;
    if (show) shareChart.show(i);
    else shareChart.hide(i);
  });
  const cats = shareChart.data.datasets.map(d=>d.label);
  renderShareLegend(cats);
}

// ── 初始化 ──
function init() {
  const yesterday = getOffsetDate(-1);
  document.getElementById('dateStart').value = yesterday;
  document.getElementById('dateEnd').value = yesterday;
  // 填充分类下拉
  const sel = document.getElementById('catSelect');
  CATEGORIES.forEach(c => {
    const o = document.createElement('option');
    o.value = c; o.textContent = c;
    sel.appendChild(o);
  });
  onQuery();
}

function getOffsetDate(offset) {
  const d = new Date();
  d.setDate(d.getDate() + offset);
  return d.toISOString().slice(0,10);
}

function setQuickDate(type) {
  const today = new Date();
  const fmt = d => d.toISOString().slice(0,10);
  if (type === 'yesterday') {
    const y = new Date(today); y.setDate(y.getDate()-1);
    document.getElementById('dateStart').value = fmt(y);
    document.getElementById('dateEnd').value = fmt(y);
  } else if (type === 'week') {
    const s = new Date(today); s.setDate(s.getDate()-7);
    document.getElementById('dateStart').value = fmt(s);
    document.getElementById('dateEnd').value = fmt(new Date(today));
  } else if (type === 'month') {
    const s = new Date(today); s.setDate(s.getDate()-30);
    document.getElementById('dateStart').value = fmt(s);
    document.getElementById('dateEnd').value = fmt(new Date(today));
  }
  onQuery();
}

// ── 查询 ──
async function onQuery() {
  const ds = document.getElementById('dateStart').value;
  const de = document.getElementById('dateEnd').value;
  if (!ds || !de) { alert('请选择日期范围'); return; }

  document.getElementById('tableBody').innerHTML = '<tr><td colspan="18" class="loading">⏳ 加载中...</td></tr>';
  document.getElementById('statsBar').innerHTML = '<span style="color:#aaa">统计中...</span>';
  activeCategory = '';
  document.getElementById('catSelect').value = '';
  searchText = '';
  document.getElementById('searchBox').value = '';

  try {
    const res = await fetch(`/api/feedback?date_start=${ds}&date_end=${de}`);
    const json = await res.json();
    if (json.error) throw new Error(json.error);

    allData = json.data || [];
    document.getElementById('infoDesc').innerHTML =
      `日期：<strong>${ds}${ds!==de?' 至 '+de:''}</strong> &nbsp;|&nbsp; 有效反馈：<strong>${json.total}</strong> 条 &nbsp;|&nbsp; 过滤无效：<strong>${json.invalid_count}</strong> 条`;

    applyFilter();
    renderStats();
    // 如果当前在趋势/占比页，同步刷新
    if (currentTab === 'trend') loadTrend();
    if (currentTab === 'share') loadShare();
  } catch(e) {
    document.getElementById('tableBody').innerHTML = `<tr><td colspan="18" class="empty">❌ ${e.message}</td></tr>`;
  }
}

// ── 统计 chips ──
function renderStats() {
  const stats = {};
  allData.forEach(r => { stats[r.category] = (stats[r.category]||0)+1; });
  const list = Object.entries(stats).sort((a,b)=>b[1]-a[1]);

  let html = `<span class="chip active" id="chip-all" onclick="filterCat('')">全部 <span class="cnt">${allData.length}</span></span>`;
  list.forEach(([cat, cnt]) => {
    html += `<span class="chip" id="chip-${encodeURIComponent(cat)}" onclick="filterCat('${cat.replace(/'/g,"\\'")}')">
      ${cat} <span class="cnt">${cnt}</span></span>`;
  });
  document.getElementById('statsBar').innerHTML = html;
}

function filterCat(cat) {
  activeCategory = cat;
  currentPage = 1;
  // 同步下拉
  document.getElementById('catSelect').value = cat;
  // 同步chips样式
  document.querySelectorAll('.chip').forEach(el => el.classList.remove('active'));
  const id = cat ? 'chip-'+encodeURIComponent(cat) : 'chip-all';
  const el = document.getElementById(id);
  if (el) el.classList.add('active');
  applyFilter();
}

function onCatSelect() {
  filterCat(document.getElementById('catSelect').value);
}

function onSearch() {
  searchText = document.getElementById('searchBox').value.trim().toLowerCase();
  currentPage = 1;
  applyFilter();
}

function applyFilter() {
  filteredData = allData.filter(r => {
    const catOk = !activeCategory || r.category === activeCategory;
    const word = (r.word||'').toLowerCase();
    const srchOk = !searchText || word.includes(searchText);
    const trynoOk = [1509, 1517, 1335].includes(Number(r.tryno));
    return catOk && srchOk && trynoOk;
  });
  renderTable();
  renderPagination();
}

// ── 渲染表格 ──
function renderTable() {
  const start = (currentPage-1)*pageSize;
  const rows = pageSize >= 9999 ? filteredData : filteredData.slice(start, start+pageSize);
  if (!rows.length) {
    document.getElementById('tableBody').innerHTML = '<tr><td colspan="18" class="empty">暂无数据</td></tr>';
    return;
  }
  let html = '';
  rows.forEach((r, idx) => {
    const contact = (r.tel && r.tel !== 'NULL') ? r.tel : (r.wechat && r.wechat !== 'NULL') ? '微信:'+r.wechat : (r.qqnum && r.qqnum !== 'NULL') ? r.qqnum : '-';
    const isvip = r.isvip==1 ? '<span class="tag-vip">会员</span>' : '<span class="tag-normal">普通</span>';
    const osName = OS_MAP[r.os] || r.os || '-';
    const word = (r.word||'').replace(/</g,'&lt;').replace(/>/g,'&gt;');
    const wordRaw = (r.word||'').replace(/"/g,'&quot;');
    const svrid = r.svrid||'-';
    const rowId = `row-${start+idx}`;
    const dataIdx = start+idx;
    const isInvalid = r._invalid || false;
    const catOptions = CATEGORIES.map(c =>
      `<option value="${c}"${c===r.category?' selected':''}>${c}</option>`
    ).join('');
    const fmtContact = (v) => (v && v !== 'NULL' && v !== 'null') ? v : '-';
    html += `<tr id="${rowId}" class="${isInvalid?'row-invalid':''}">
      <td>${r.date||'-'}</td>
      <td>${r.src_from||'-'}</td>
      <td>${r.v_from||'-'}</td>
      <td>${r.tryno||'-'}</td>
      <td>${r.tid1||'-'}</td><td>${r.tid2||'-'}</td><td>${r.tod1||'-'}</td><td>${r.tod2||'-'}</td>
      <td>${r.days??'-'}</td>
      <td title="${svrid}">
        <span class="svrid-text">${svrid.length>14?svrid.slice(0,14)+'…':svrid}</span>
        <button class="btn-copy" title="复制svrid" data-svrid="${svrid}" onclick="copySvrId(this)">📋</button>
      </td>
      <td class="word-col">${word}</td>
      <td>${fmtContact(r.qqnum)}</td>
      <td>${fmtContact(r.wechat)}</td>
      <td>${fmtContact(r.tel)}</td>
      <td>${isvip}</td>
      <td>${r.time||'-'}</td>
      <td>${osName}</td>
      <td style="white-space:nowrap">
        <select class="cat-select" data-word="${wordRaw}" data-idx="${dataIdx}"
          onchange="saveCat(this)">
          ${catOptions}
        </select>
        <span class="save-tip" id="tip-${dataIdx}">✓ 已保存</span>
      </td>
      <td style="text-align:center;white-space:nowrap">
        <button class="btn-invalid${isInvalid?' marked':''}" title="${isInvalid?'撤销无效标记':'标记为无效反馈'}"
          data-word="${wordRaw}" data-idx="${dataIdx}" onclick="toggleInvalid(this)">🚫</button>
      </td>
    </tr>`;
  });
  document.getElementById('tableBody').innerHTML = html;
}

// ── 分页 ──
function renderPagination() {
  if (pageSize >= 9999) { document.getElementById('pagination').innerHTML=''; return; }
  const total = filteredData.length;
  const totalPages = Math.ceil(total/pageSize)||1;
  const pag = document.getElementById('pagination');
  if (totalPages<=1) { pag.innerHTML=''; return; }

  let html = '';
  if (currentPage>1) html += `<button onclick="goPage(${currentPage-1})">‹</button>`;
  let s=Math.max(1,currentPage-3), e=Math.min(totalPages,currentPage+3);
  if (s>1) html+=`<button onclick="goPage(1)">1</button><span>…</span>`;
  for(let i=s;i<=e;i++) html+=`<button class="${i===currentPage?'active':''}" onclick="goPage(${i})">${i}</button>`;
  if (e<totalPages) html+=`<span>…</span><button onclick="goPage(${totalPages})">${totalPages}</button>`;
  if (currentPage<totalPages) html+=`<button onclick="goPage(${currentPage+1})">›</button>`;
  html += `<span class="pinfo">共 ${total} 条 / ${totalPages} 页</span>`;
  pag.innerHTML = html;
}

function goPage(p) { currentPage=p; renderTable(); renderPagination(); window.scrollTo(0,0); }
function onPageSizeChange() { pageSize=parseInt(document.getElementById('pageSizeSelect').value); currentPage=1; renderTable(); renderPagination(); }

// ── 导出 CSV ──
function exportCSV() {
  const headers = ['日期','tid1','tid2','tod1','tod2','安装天数','svrid信息','用户反馈内容','用户联系方式','是否会员','卸载时间','系统','分类'];
  const esc = v => '"'+(v||'').toString().replace(/"/g,'""')+'"';
  const rows = [headers.join(',')];
  filteredData.forEach(r => {
    const contact = (r.tel&&r.tel!=='NULL')?r.tel:(r.wechat&&r.wechat!=='NULL')?'微信:'+r.wechat:(r.qqnum&&r.qqnum!=='NULL')?r.qqnum:'';
    rows.push([
      r.date,r.tid1,r.tid2,r.tod1,r.tod2,r.days??'',
      esc(r.word),contact,
      r.isvip==1?'会员':'普通',
      r.time, OS_MAP[r.os]||r.os||'',
      r.category
    ].join(','));
  });
  const ds = document.getElementById('dateStart').value;
  const de = document.getElementById('dateEnd').value;
  const blob = new Blob(['\uFEFF'+rows.join('\n')],{type:'text/csv;charset=utf-8'});
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = `毒霸卸载反馈_${ds}${ds!==de?'_至_'+de:''}.csv`;
  a.click();
}


// ── 手动标记无效 ──
async function toggleInvalid(btn) {
  const word = btn.getAttribute('data-word');
  const idx = parseInt(btn.getAttribute('data-idx'));
  const isMarked = btn.classList.contains('marked');
  const row = document.getElementById('row-' + idx);

  btn.style.opacity = '0.3';
  btn.style.pointerEvents = 'none';

  try {
    const api = isMarked ? '/api/mark_valid' : '/api/mark_invalid';
    const res = await fetch(api, {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ word, reason: isMarked ? '' : '手动标记' })
    });
    const json = await res.json();
    if (!json.ok) throw new Error(json.error || '操作失败');

    // 更新内存
    const dataItem = allData[idx];
    if (dataItem) dataItem._invalid = !isMarked;

    // 更新按钮和行样式
    if (isMarked) {
      btn.classList.remove('marked');
      btn.title = '标记为无效反馈';
      row && row.classList.remove('row-invalid');
    } else {
      btn.classList.add('marked');
      btn.title = '撤销无效标记';
      row && row.classList.add('row-invalid');
    }
  } catch(e) {
    alert('操作失败：' + e.message);
  } finally {
    btn.style.opacity = '';
    btn.style.pointerEvents = '';
  }
}

// ── 手动修改分类 ──
function copySvrId(btn) {
  const svrid = btn.getAttribute('data-svrid');
  if (!svrid) return;
  const ok = () => { btn.textContent = '✅'; setTimeout(() => btn.textContent = '📋', 1200); };
  if (navigator.clipboard && navigator.clipboard.writeText) {
    navigator.clipboard.writeText(svrid).then(ok).catch(() => fallbackCopy(svrid, ok));
  } else {
    fallbackCopy(svrid, ok);
  }
}
function fallbackCopy(text, cb) {
  const ta = document.createElement('textarea');
  ta.value = text;
  ta.style.position = 'fixed'; ta.style.opacity = '0';
  document.body.appendChild(ta);
  ta.focus(); ta.select();
  try { document.execCommand('copy'); cb(); } catch(e) { alert('复制失败: ' + text); }
  document.body.removeChild(ta);
}

async function saveCat(sel) {
  const word = sel.getAttribute('data-word');
  const idx = sel.getAttribute('data-idx');
  const category = sel.value;
  const tip = document.getElementById('tip-' + idx);

  sel.classList.add('saving');
  if (tip) { tip.classList.remove('show'); }

  try {
    const res = await fetch('/api/reclassify', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({word, category})
    });
    const json = await res.json();
    if (!json.ok) throw new Error(json.error || '保存失败');

    // 同步更新内存数据
    const dataIdx = parseInt(idx);
    if (allData[dataIdx]) allData[dataIdx].category = category;

    sel.classList.remove('saving');
    sel.classList.add('saved');
    if (tip) { tip.classList.add('show'); setTimeout(() => { tip.classList.remove('show'); sel.classList.remove('saved'); }, 2000); }

    // 刷新统计 chips（不重新请求接口）
    renderStats();
  } catch(e) {
    sel.classList.remove('saving');
    alert('保存失败：' + e.message);
  }
}

init();
</script>
</body>
</html>
'''

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5002, debug=False)
