import sqlite3

c = sqlite3.connect("/root/ppanel-agent/data/ppanel.db")
print("instances:", c.execute("select id,name,ext_port,status from instances").fetchall())
print("users:", c.execute("select id,username from users").fetchall())
