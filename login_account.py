# -*- coding: utf-8 -*-
"""Одноразовий вхід у Telegram. Запустити один раз: python login.py"""
import os, sys
from telethon import TelegramClient
import config

BASE = os.path.dirname(os.path.abspath(__file__))

if not config.API_ID or not config.API_HASH:
    sys.exit("❌ Спершу впишіть API_ID і API_HASH у config.py")

with TelegramClient(os.path.join(BASE, "session_user"),
                    config.API_ID, config.API_HASH) as client:
    me = client.get_me()
    print("\n✅ Вхід виконано: %s (@%s)" % (me.first_name, me.username or "—"))
    print("   Файл session_user.session створено. Більше цей крок не потрібен.\n")
