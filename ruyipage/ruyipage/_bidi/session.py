# -*- coding: utf-8 -*-
"""BiDi session 模块命令"""


def status(driver):
    """查询远程端状态

    Returns:
        {'ready': bool, 'message': str}
    """
    return driver.run("session.status")


def new(driver, capabilities=None, user_prompt_handler=None):
    """创建新会话

    Args:
        capabilities: 能力请求字典
        user_prompt_handler: 可选，session.UserPromptHandler 字典

    Returns:
        {'sessionId': str, 'capabilities': dict}
    """
    caps = dict(capabilities or {})
    if user_prompt_handler:
        always_match = dict(caps.get("alwaysMatch", {}))
        always_match["unhandledPromptBehavior"] = dict(user_prompt_handler)
        caps["alwaysMatch"] = always_match
    params = {"capabilities": caps}
    return driver.run("session.new", params)


def end(driver):
    """结束当前会话"""
    return driver.run("session.end")


def subscribe(driver, events, contexts=None):
    """订阅事件

    Args:
        events: 事件名列表，如 ['network.responseCompleted', 'log.entryAdded']
                也可以是模块名，如 ['network'] 订阅该模块所有事件
        contexts: 可选，限定 context 列表

    Returns:
        {'subscription': str}  订阅 ID
    """
    params = {"events": events if isinstance(events, list) else [events]}
    if contexts:
        params["contexts"] = contexts if isinstance(contexts, list) else [contexts]
    return driver.run("session.subscribe", params)


def unsubscribe(driver, events=None, contexts=None, subscription=None):
    """取消订阅事件

    Args:
        events: 事件名列表
        contexts: 可选，限定 context 列表
        subscription: 可选，通过订阅 ID 取消
    """
    params = {}
    if subscription:
        params["subscriptions"] = (
            [subscription] if isinstance(subscription, str) else subscription
        )
    else:
        if events:
            params["events"] = events if isinstance(events, list) else [events]
        if contexts:
            params["contexts"] = contexts if isinstance(contexts, list) else [contexts]
    return driver.run("session.unsubscribe", params)
