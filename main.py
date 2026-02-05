import sqlite3
import asyncio
import logging
from datetime import datetime, timedelta
from typing import Dict, Set, List

import astrbot.api.event as event
from astrbot.api.event import filter, AstrMessageEvent, MessageEventResult
from astrbot.api.star import Context, Star, register
import astrbot.api.message_components as Comp

# 配置日志
logger = logging.getLogger("astrbot_plugin_qinghuo")
logger.setLevel(logging.INFO)

# 数据库路径 - 按AstrBot规范，持久化数据存到data目录
DB_PATH = "data/qinghuo.db"

# 内存数据结构
subscribed_users: Set[int] = set()
user_data: Dict[int, Dict[str, object]] = {}
group_users: Dict[int, List[int]] = {}

# -------------------------------
# 数据库相关操作
# -------------------------------
def get_conn():
    return sqlite3.connect(DB_PATH)

def init_db():
    conn = get_conn()
    cursor = conn.cursor()
    # 用户体力表
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            stamina_max INTEGER DEFAULT 15,
            stamina_current INTEGER DEFAULT 0,
            last_update TEXT,
            daily_challenge INTEGER DEFAULT 0,
            notified INTEGER DEFAULT 0
        )
    """)
    # 群组用户关系表
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS group_users (
            group_id INTEGER,
            user_id INTEGER,
            PRIMARY KEY (group_id, user_id)
        )
    """)
    conn.commit()
    conn.close()

def load_data_from_db():
    conn = get_conn()
    cursor = conn.cursor()
    # 加载用户数据
    cursor.execute("SELECT user_id, stamina_max, stamina_current, last_update, daily_challenge, notified FROM users")
    rows = cursor.fetchall()
    for row in rows:
        user_id = row[0]
        subscribed_users.add(user_id)
        last_update = datetime.fromisoformat(row[3]) if row[3] else datetime.now()
        user_data[user_id] = {
            "max": row[1],
            "current": row[2],
            "last_update": last_update,
            "daily_challenge": bool(row[4]),
            "notified": bool(row[5]),
        }
    # 加载群组关系
    cursor.execute("SELECT group_id, user_id FROM group_users")
    rows = cursor.fetchall()
    for group_id, user_id in rows:
        if group_id not in group_users:
            group_users[group_id] = []
        if user_id not in group_users[group_id]:
            group_users[group_id].append(user_id)
    conn.close()

def save_user_to_db(user_id: int):
    if user_id not in user_data:
        return
    data = user_data[user_id]
    conn = get_conn()
    cursor = conn.cursor()
    cursor.execute("""
        INSERT INTO users (user_id, stamina_max, stamina_current, last_update, daily_challenge, notified)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(user_id) DO UPDATE SET
            stamina_max=excluded.stamina_max,
            stamina_current=excluded.stamina_current,
            last_update=excluded.last_update,
            daily_challenge=excluded.daily_challenge,
            notified=excluded.notified
    """, (
        user_id,
        data["max"],
        data["current"],
        data["last_update"].isoformat(),
        int(data.get("daily_challenge", False)),
        int(data.get("notified", False))
    ))
    conn.commit()
    conn.close()

def remove_user_from_db(user_id: int):
    conn = get_conn()
    cursor = conn.cursor()
    cursor.execute("DELETE FROM users WHERE user_id = ?", (user_id,))
    cursor.execute("DELETE FROM group_users WHERE user_id = ?", (user_id,))
    conn.commit()
    conn.close()

def add_group_user_to_db(group_id: int, user_id: int):
    conn = get_conn()
    cursor = conn.cursor()
    cursor.execute("INSERT OR IGNORE INTO group_users (group_id, user_id) VALUES (?, ?)", (group_id, user_id))
    conn.commit()
    conn.close()

def remove_group_user_from_db(group_id: int, user_id: int):
    conn = get_conn()
    cursor = conn.cursor()
    cursor.execute("DELETE FROM group_users WHERE group_id = ? AND user_id = ?", (group_id, user_id))
    conn.commit()
    conn.close()

# -------------------------------
# 工具函数
# -------------------------------
def calculate_next_recovery(user_id: int) -> str:
    data = user_data.get(user_id)
    if not data:
        return "未订阅"
    last_update = data["last_update"]
    next_recovery = last_update + timedelta(minutes=30)
    now = datetime.now()
    if next_recovery < now:
        return "即将恢复"
    return next_recovery.strftime("%H:%M")

def calculate_full_time(user_id: int) -> str:
    data = user_data.get(user_id)
    if not data:
        return "尚无数据"
    current = data["current"]
    max_stamina = data["max"]
    if current >= max_stamina:
        return "已满体力"
    last_update = data["last_update"]
    remain = max_stamina - current
    full_time = last_update + timedelta(minutes=remain * 30)
    return full_time.strftime("%Y-%m-%d %H:%M:%S")

# -------------------------------
# 业务逻辑函数（增加self判空兜底）
# -------------------------------
async def send_reminder(self, user_id: int, message: str):
    """发送提醒消息"""
    if not self or not hasattr(self, 'context'):
        return
    for group_id, users in group_users.items():
        if user_id in users:
            chain = [
                Comp.At(qq=str(user_id)),
                Comp.Plain(text=f"\n🎤 {message} - sou桑")
            ]
            try:
                await self.context.send_message(
                    unified_msg_origin=f"group_{group_id}",
                    chains=chain
                )
            except Exception as e:
                logger.error(f"发送提醒给用户 {user_id} 失败: {str(e)}")
            break

async def check_stamina(self, user_id: int):
    """检查体力恢复状态"""
    if not self or user_id not in subscribed_users or user_id not in user_data:
        return
    data = user_data[user_id]
    now = datetime.now()
    if data["current"] >= data["max"] and data.get("notified", False):
        return
    time_diff = now - data["last_update"]
    recover_points = int(time_diff.total_seconds() // 1800)
    if recover_points > 0:
        new_current = min(data["current"] + recover_points, data["max"])
        if new_current > data["current"]:
            data["current"] = new_current
            remainder = time_diff.total_seconds() % 1800
            data["last_update"] = now - timedelta(seconds=remainder)
            save_user_to_db(user_id)
    if data["current"] >= data["max"] and not data.get("notified", False):
        data["notified"] = True
        save_user_to_db(user_id)
        await send_reminder(self, user_id, "你的体力已经满啦！快去打歌吧~")

async def send_daily_challenge_reminders(self):
    """发送每日挑战提醒"""
    if not self or not hasattr(self, 'context'):
        return
    users_to_remind = [uid for uid in subscribed_users if not user_data.get(uid, {}).get("daily_challenge", False)]
    for group_id, users in group_users.items():
        group_users_to_remind = [uid for uid in users_to_remind if uid in users]
        if not group_users_to_remind:
            continue
        chain = [Comp.Plain(text="📢 每日挑战提醒（22:00）\n")]
        for uid in group_users_to_remind:
            chain.append(Comp.At(qq=str(uid)))
        chain.append(Comp.Plain(text="\n你们今天还没有完成每日挑战哦，sou桑提醒大家不要忘记哦～"))
        try:
            await self.context.send_message(
                unified_msg_origin=f"group_{group_id}",
                chains=chain
            )
        except Exception as e:
            logger.error(f"发送群 {group_id} 每日挑战提醒失败: {str(e)}")

# -------------------------------
# 插件主体（AstrBot v4.14.4终极适配）
# -------------------------------
@register("qinghuo", "luban652", "体力恢复提醒+每日挑战打卡插件", "1.0.0")
class QinghuoPlugin(Star):
    def __init__(self, context: Context):
        super().__init__(context)
        # 初始化数据库和数据
        init_db()
        load_data_from_db()
        # 延迟启动定时任务，避免初始化阶段方法解析冲突
        asyncio.create_task(self.delay_start_tasks())
        logger.info("清火插件初始化完成，即将启动定时任务")

    async def delay_start_tasks(self):
        """延迟1秒启动定时任务，确保类所有方法完全解析"""
        await asyncio.sleep(1)
        asyncio.create_task(self.stamina_check_task())
        asyncio.create_task(self.daily_reminder_task())
        asyncio.create_task(self.daily_reset_task())
        logger.info("清火插件定时任务全部启动成功")

    # -------------------------------
    # 定时任务方法（类内成员方法）
    # -------------------------------
    async def stamina_check_task(self):
        """每分钟检查体力恢复状态"""
        check_interval = 60
        max_retries = 3
        while True:
            try:
                logger.info(f"开始体力检查 (时间: {datetime.now().isoformat()})")
                current_users = list(subscribed_users)
                for user_id in current_users:
                    retry_count = 0
                    while retry_count < max_retries:
                        try:
                            await check_stamina(self, user_id)
                            break
                        except Exception as e:
                            retry_count += 1
                            logger.error(f"用户 {user_id} 体力检查失败 (重试 {retry_count}/{max_retries}): {str(e)}")
                            await asyncio.sleep(5)
                logger.info(f"体力检查完成，下次检查在 {check_interval} 秒后")
                await asyncio.sleep(check_interval)
            except asyncio.CancelledError:
                logger.info("体力检查任务被取消")
                break
            except Exception as e:
                logger.critical(f"体力检查任务异常: {str(e)}", exc_info=True)
                await asyncio.sleep(30)

    async def daily_reminder_task(self):
        """每日22:00发送每日挑战未完成提醒"""
        while True:
            try:
                now = datetime.now()
                target = now.replace(hour=22, minute=0, second=0, microsecond=0)
                if now >= target:
                    target += timedelta(days=1)
                await asyncio.sleep((target - now).total_seconds())
                await send_daily_challenge_reminders(self)
            except asyncio.CancelledError:
                logger.info("每日挑战提醒任务被取消")
                break
            except Exception as e:
                logger.error(f"每日挑战提醒任务异常: {str(e)}", exc_info=True)
                await asyncio.sleep(60)

    async def daily_reset_task(self):
        """每日4:00重置用户每日挑战状态"""
        while True:
            try:
                now = datetime.now()
                target = now.replace(hour=4, minute=0, second=0, microsecond=0)
                if now >= target:
                    target += timedelta(days=1)
                await asyncio.sleep((target - now).total_seconds())
                for user_id in subscribed_users:
                    data = user_data.get(user_id)
                    if data:
                        data["daily_challenge"] = False
                        save_user_to_db(user_id)
                logger.info("每日挑战状态已全局重置")
            except asyncio.CancelledError:
                logger.info("每日挑战重置任务被取消")
                break
            except Exception as e:
                logger.error(f"每日挑战重置任务异常: {str(e)}", exc_info=True)
                await asyncio.sleep(60)

    # -------------------------------
    # 指令组与功能指令
    # -------------------------------
    @filter.command_group("清火", alias={"体力提醒"})
    def qinghuo_group(self):
        """清火插件指令组"""
        pass

    @qinghuo_group.command("帮助")
    async def help_cmd(self, event: AstrMessageEvent):
        """查看插件所有指令帮助"""
        help_text = (
            "🎤 sou桑体力提醒指令帮助：\n"
            "/清火 订阅 - 订阅体力提醒服务\n"
            "/清火 取消订阅 - 取消体力提醒服务\n"
            "/清火 设置上限 <15-50> - 自定义体力上限（默认15）\n"
            "/清火 设置当前 <体力值> <冷却分钟> - 手动设置体力和冷却时间\n"
            "/清火 每日挑战 - 标记今日每日挑战已完成\n"
            "/清火 查看 - 查看个人当前体力状态\n"
            "/清火 清火 - 手动清空体力（开始恢复）\n"
            "/清火 帮助 - 查看本帮助信息"
        )
        yield event.plain_result(help_text)

    @qinghuo_group.command("订阅")
    async def subscribe_cmd(self, event: AstrMessageEvent):
        """订阅体力提醒服务"""
        user_id = int(event.get_sender_id())
        if user_id in subscribed_users:
            yield event.plain_result("🎤 你已经订阅过sou桑的体力提醒服务啦～")
            return
        # 初始化用户数据
        subscribed_users.add(user_id)
        user_data[user_id] = {
            "max": 15,
            "current": 0,
            "last_update": datetime.now(),
            "daily_challenge": False,
            "notified": False
        }
        save_user_to_db(user_id)
        # 绑定群关系
        group_id = event.message_obj.group_id
        if group_id:
            group_id = int(group_id)
            if group_id not in group_users:
                group_users[group_id] = []
            group_users[group_id].append(user_id)
            add_group_user_to_db(group_id, user_id)
        yield event.plain_result("🎤 好耶！sou桑会好好提醒你清体力的～\n默认体力上限15点，可用/清火 设置上限 修改哦～")

    @qinghuo_group.command("取消订阅")
    async def unsubscribe_cmd(self, event: AstrMessageEvent):
        """取消体力提醒服务"""
        user_id = int(event.get_sender_id())
        if user_id not in subscribed_users:
            yield event.plain_result("🎤 诶...你还没有订阅sou桑的服务呢...")
            return
        # 清理用户数据
        subscribed_users.remove(user_id)
        user_data.pop(user_id, None)
        remove_user_from_db(user_id)
        # 解绑群关系
        group_id = event.message_obj.group_id
        if group_id:
            group_id = int(group_id)
            if group_id in group_users and user_id in group_users[group_id]:
                group_users[group_id].remove(user_id)
                remove_group_user_from_db(group_id, user_id)
        yield event.plain_result("🎤 ...sou桑会想你的...\n期待与你的下次相遇～")

    @qinghuo_group.command("设置上限")
    async def set_max_cmd(self, event: AstrMessageEvent, stamina: int):
        """设置体力上限（15-50）"""
        user_id = int(event.get_sender_id())
        if user_id not in subscribed_users:
            yield event.plain_result("🎤 那个...请先订阅sou桑的服务哦～")
            return
        if not 15 <= stamina <= 50:
            yield event.plain_result("🎤 体力上限需要设置在15-50之间哦～")
            return
        user_data[user_id]["max"] = stamina
        save_user_to_db(user_id)
        yield event.plain_result(f"🎤 了解！sou桑已经记住你的体力上限是 {stamina} 点啦！")

    @qinghuo_group.command("设置当前")
    async def set_current_cmd(self, event: AstrMessageEvent, current: int, cooldown: int):
        """手动设置当前体力和冷却时间"""
        user_id = int(event.get_sender_id())
        if user_id not in subscribed_users:
            yield event.plain_result("🎤 请先订阅服务哦~")
            return
        max_stamina = user_data[user_id]["max"]
        # 校验参数
        if current < 0 or current > max_stamina:
            yield event.plain_result(f"🎤 当前体力需要设置在0-{max_stamina}之间啦～")
            return
        if cooldown < 0 or cooldown >= 30:
            yield event.plain_result("🎤 冷却时间需要设置在0-30分钟之间哦～")
            return
        # 更新数据
        user_data[user_id]["current"] = current
        user_data[user_id]["last_update"] = datetime.now() - timedelta(minutes=(30 - cooldown) if cooldown > 0 else 0)
        user_data[user_id]["notified"] = False
        save_user_to_db(user_id)
        full_time = calculate_full_time(user_id)
        yield event.plain_result(
            f"🎤 好！sou桑已经记录好啦～\n"
            f"当前体力: {current}\n"
            f"冷却剩余: {cooldown}分钟\n"
            f"预计满体力时间: {full_time}\n"
        )

    @qinghuo_group.command("每日挑战")
    async def daily_challenge_cmd(self, event: AstrMessageEvent):
        """标记每日挑战已完成"""
        user_id = int(event.get_sender_id())
        if user_id not in subscribed_users:
            yield event.plain_result("🎤 请先订阅服务哦~")
            return
        user_data[user_id]["daily_challenge"] = True
        save_user_to_db(user_id)
        yield event.plain_result("🎤 你已经完成今日的每日挑战了！真厉害！")

    @qinghuo_group.command("查看")
    async def check_cmd(self, event: AstrMessageEvent):
        """查看个人当前体力状态"""
        user_id = int(event.get_sender_id())
        if user_id not in subscribed_users:
            yield event.plain_result("🎤 请先订阅服务哦~")
            return
        data = user_data[user_id]
        next_recovery = calculate_next_recovery(user_id)
        full_time = calculate_full_time(user_id)
        daily_status = "完成啦！真厉害" if data.get("daily_challenge", False) else "还没完成哦！"
        msg = (
            f"🎤 主人主人！\n"
            f"⚡ 你的体力信息：\n"
            f"🔋 体力上限: {data['max']}\n"
            f"💚 当前体力: {data['current']}\n"
            f"⏱️ 下次恢复: {next_recovery}\n"
            f"🕒 预计满体力时间: {full_time}\n"
            f"📌 每日挑战: {daily_status}"
        )
        yield event.plain_result(msg)

    @qinghuo_group.command("清火")
    async def clear_cmd(self, event: AstrMessageEvent):
        """手动清空体力，开始重新恢复"""
        user_id = int(event.get_sender_id())
        if user_id not in subscribed_users:
            yield event.plain_result("🎤 请先订阅sou桑的服务哦~")
            return
        user_data[user_id]["current"] = 0
        user_data[user_id]["last_update"] = datetime.now()
        user_data[user_id]["notified"] = False
        save_user_to_db(user_id)
        yield event.plain_result("🎤 清火成功！sou桑会继续为你记录体力恢复哦~")

    async def terminate(self):
        """插件卸载时执行清理"""
        logger.info("清火插件已卸载，感谢使用～")