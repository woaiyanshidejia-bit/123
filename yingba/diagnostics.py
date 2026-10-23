import platform
import sqlite3
from .adapters import CodexSubscription

def diagnose(store):
    with store.connect() as db: integrity=db.execute('PRAGMA integrity_check').fetchone()[0]
    return {'程序':'YINGBA 1.0.0','系统':platform.system()+' '+platform.release(),'Python':platform.python_version(),
            '数据库':integrity,'Apollo':{'connected':False,'reason':'本地真实适配器未接通，未调用付费操作'},
            'Codex':CodexSubscription().status(),'核实适配器':{'connected':False,'reason':'真实网页核实和订阅联调待本机验收'},
            '预算':'必须核实北京时间全账号当日用量；本地账本不能观察其它应用扣费',
            '验收':'Windows 10、浏览器登录、真实Apollo及真实订阅调用未在本云端验收'}
