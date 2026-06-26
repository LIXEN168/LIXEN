"""
沪铜期货价格监控与推送（GitHub Actions 云端版）
每天北京时间 08:00 执行，周一至周六
"""
import requests
import json
import os
import re
from datetime import datetime, timedelta

# ========== 配置 ==========
WEBHOOK_URL = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=2fecf72e-96fa-4ba1-9a50-ab490f9c6319"
RECORD_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "copper_record.txt")

# ========== 第一步：获取沪铜实时价格 ==========
def get_shfe_copper():
    """
    获取沪铜主力最新价，多层回退：
    1. playwright 抓取金投网（最准确）
    2. 新浪财经API - CU0连续合约
    3. 百度搜索提取
    """
    # 方案1：playwright 抓取金投网页面
    try:
        from playwright.sync_api import sync_playwright
        from bs4 import BeautifulSoup
        
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True, args=['--no-sandbox'])
            page = browser.new_page()
            page.goto("https://www.cngold.org/qihuo/hutong.html", timeout=30000, wait_until="networkidle")
            html = page.content()
            browser.close()
        
        soup = BeautifulSoup(html, "html.parser")
        # 查找价格元素 - 金投网页面通常有 class 包含 price/latest 的元素
        for selector in [".price", ".latest", ".js_price", "[class*='price']", "span.red", ".num"]:
            elems = soup.select(selector)
            for elem in elems:
                text = elem.get_text(strip=True).replace(",", "")
                match = re.search(r'(\d{5,6})', text)
                if match:
                    price = int(match.group(1))
                    if 70000 < price < 200000:
                        print(f"  [playwright] 从 '{selector}' 提取到: {price}")
                        return price
        
        # 如果选择器没命中，全页面搜索
        text = soup.get_text()
        prices = re.findall(r'(\d{5,6})', text)
        valid = [int(p) for p in prices if 70000 < int(p) < 200000]
        if valid:
            from collections import Counter
            price = Counter(valid).most_common(1)[0][0]
            print(f"  [playwright] 全页搜索最频价格: {price}")
            return price
    except Exception as e:
        print(f"  [playwright] 失败: {e}")
    
    # 方案2：新浪财经API - CU0
    try:
        url = "https://hq.sinajs.cn/list=nf_CU0"
        headers = {"Referer": "https://finance.sina.com.cn", "User-Agent": "Mozilla/5.0"}
        resp = requests.get(url, headers=headers, timeout=10)
        resp.encoding = "gbk"
        match = re.search(r'"([^"]+)"', resp.text)
        if match:
            parts = match.group(1).split(",")
            # 新浪期货字段：0名称,1最新价,2今开,3昨收...
            # CU0连续合约字段特殊，尝试多个索引
            for idx in [1, 8, 2]:  # 最新价 > 买一价 > 今开
                try:
                    price = int(float(parts[idx]))
                    if 70000 < price < 200000:
                        print(f"  [sina-CU0] 索引{idx}: {price}")
                        return price
                except (ValueError, IndexError):
                    continue
    except Exception as e:
        print(f"  [sina-CU0] 失败: {e}")
    
    # 方案3：百度搜索
    try:
        search_url = "https://www.baidu.com/s?wd=沪铜主力 最新价"
        headers = {"User-Agent": "Mozilla/5.0"}
        resp = requests.get(search_url, headers=headers, timeout=15)
        matches = re.findall(r'(\d{5,6})\s*元', resp.text)
        for m in matches:
            price = int(m)
            if 70000 < price < 200000:
                print(f"  [baidu] 提取到: {price}")
                return price
    except Exception as e:
        print(f"  [baidu] 失败: {e}")
    
    return None

# ========== 第二步：获取长江1#铜D-2日均价 ==========
def get_changjiang_avg_price(target_date_str):
    """
    获取D-2日长江1#铜日均价
    target_date_str: 格式 2026-06-24
    回退链：百度搜索 → 新浪文章 → 新浪行情页
    """
    # 策略1：百度搜索
    try:
        search_url = f"https://www.baidu.com/s?wd=长江有色 {target_date_str} 1#铜 均价"
        headers = {"User-Agent": "Mozilla/5.0"}
        resp = requests.get(search_url, headers=headers, timeout=15)
        matches = re.findall(r'(\d{5,6})\s*(?:元|￥)', resp.text)
        if matches:
            prices = [int(m) for m in matches if 60000 < int(m) < 150000]
            if prices:
                from collections import Counter
                price = Counter(prices).most_common(1)[0][0]
                print(f"  [baidu] 长江均价: {price}")
                return price
    except Exception as e:
        print(f"  [baidu-长江] 失败: {e}")
    
    # 策略2：新浪财经搜索
    try:
        search_url = f"https://search.sina.com.cn/?q=长江现货+1%23铜+{target_date_str}&range=all&c=news"
        headers = {"User-Agent": "Mozilla/5.0"}
        resp = requests.get(search_url, headers=headers, timeout=15)
        matches = re.findall(r'(\d{5,6})\s*元/吨', resp.text)
        if matches:
            prices = [int(m) for m in matches if 60000 < int(m) < 150000]
            if prices:
                print(f"  [sina-search] 长江均价: {prices[0]}")
                return prices[0]
    except Exception as e:
        print(f"  [sina-search] 失败: {e}")
    
    return None

# ========== 第三步：计算价格档位 ==========
def calc_tier(price):
    """计算档位（单位：万元）"""
    tier = (int((price - 1001) / 2000) * 2000 + 2000) / 10000
    return tier

# ========== 第四步：读取历史记录 ==========
def read_record():
    if os.path.exists(RECORD_FILE):
        with open(RECORD_FILE, "r", encoding="utf-8") as f:
            content = f.read().strip()
            if content:
                return content
    return None

# ========== 第五步：发送 Webhook 消息 ==========
def send_webhook(msg):
    data = json.dumps({"msgtype": "text", "text": {"content": msg}}).encode("utf-8")
    req = requests.post(WEBHOOK_URL, data=data, headers={"Content-Type": "application/json"}, timeout=10)
    result = req.json()
    print(f"Webhook 发送结果: {result}")
    return result.get("errcode") == 0

# ========== 第六步：更新记录 ==========
def write_record(record_line):
    with open(RECORD_FILE, "w", encoding="utf-8") as f:
        f.write(record_line)

# ========== 主流程 ==========
def main():
    today = datetime.now()
    
    # 周日跳过
    if today.weekday() == 6:
        print("今天是周日，跳过执行")
        return
    
    # D-2日期（用于获取长江均价）
    d2_date = today - timedelta(days=2)
    d2_str = d2_date.strftime("%Y-%m-%d")
    
    print(f"执行日期: {today.strftime('%Y-%m-%d')} 星期{today.weekday()+1}")
    print(f"长江均价参考日期(D-2): {d2_str}")
    
    # 获取沪铜价格
    copper_price = get_shfe_copper()
    if not copper_price:
        print("错误：无法获取沪铜实时价格")
        return
    print(f"沪铜实时价格: {copper_price} 元")
    
    # 获取长江均价
    avg_price = get_changjiang_avg_price(d2_str)
    if not avg_price:
        print("警告：无法获取长江1#铜均价，仅发送沪铜价格")
        msg = f"截至当前实时铜价{copper_price}元，长江均价暂未获取到。"
        send_webhook(msg)
        return
    
    print(f"长江1#铜D-2日均价: {avg_price} 元")
    
    # 计算档位
    tier = calc_tier(copper_price)
    print(f"当前档位: {tier} 万元")
    
    # 读取历史记录
    old_record = read_record()
    if old_record:
        parts = old_record.split("|")
        if len(parts) >= 3:
            old_tier = float(parts[2])
            print(f"上次档位: {old_tier} 万元")
            if tier == old_tier:
                # 档位不变，仅更新记录不发送
                print("档位未变化，不发送消息")
                record_line = f"{today.strftime('%Y-%m-%d')}|{copper_price}|{tier}|{avg_price}"
                write_record(record_line)
                return
    
    # 确定星期几
    weekday = today.weekday()
    
    # 生成消息
    if copper_price > avg_price:
        msg = f"截至当前实时铜价{copper_price}元，高于上次长江现货1#铜的日均价{avg_price}元，好的产品要下单的10点前下出去，避免上涨。"
    elif copper_price < avg_price:
        if weekday == 5:  # 周六
            msg = f"截至当前实时铜价{copper_price}元，低于上次长江现货1#铜的日均价{avg_price}元，好的产品等下周一10:20铜价更新后再下单。"
        else:
            msg = f"截至当前实时铜价{copper_price}元，低于上次长江现货1#铜的日均价{avg_price}元，好的产品等10:20铜价更新后再下单。"
    else:
        msg = f"截至当前实时铜价{copper_price}元，与上次长江现货1#铜的日均价{avg_price}元持平。"
    
    print(f"消息: {msg}")
    send_webhook(msg)
    
    # 更新记录
    record_line = f"{today.strftime('%Y-%m-%d')}|{copper_price}|{tier}|{avg_price}"
    write_record(record_line)
    print(f"记录已更新: {record_line}")

if __name__ == "__main__":
    main()
