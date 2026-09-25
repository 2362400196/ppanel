import sqlite3, glob
db = sqlite3.connect(glob.glob('/opt/ppanel/agent/backend/data/*.db')[0])
print('instance_dbs:', db.execute('select id,instance_id,version,db_name,db_user,db_password from instance_dbs').fetchall())
print('op_logs(最近10):', db.execute('select action,detail,created_at from op_logs order by id desc limit 10').fetchall())
