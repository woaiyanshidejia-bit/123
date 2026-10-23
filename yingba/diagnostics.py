import platform
from . import __version__
from .adapters import CodexSubscription

def diagnose(store, runtime=None):
    with store.connect() as db:
        integrity=db.execute('PRAGMA integrity_check').fetchone()[0]
        schema=db.execute('PRAGMA user_version').fetchone()[0]
    return {'程序':'YINGBA '+__version__,'系统':platform.system()+' '+platform.release(),'Python':platform.python_version(),
            '数据库':integrity,'数据库版本':schema,
            'Apollo':runtime.status() if runtime else {'implemented':True,'health_checked':False,'reason':'官方HTTP适配器已实现；此诊断没有连接密钥'},
            'Codex':(runtime.reviewer if runtime else CodexSubscription()).status(),
            '网页核实':{'implemented':True,'end_to_end_verified':False,'reason':'HTTPS公开页面获取、逐页证据与引用校验已实现；真实网站及本人订阅联调未验收'},
            '预算':'北京时间50分；同一数据库全部任务共享。每页需核实全账号基线并停止账本外扣费；未知响应/费用冻结且不重试。',
            '验收':'云端已使用本地HTTP服务、假CLI子进程和合成公司测试。Windows10、本人登录、真实Apollo和真实订阅调用未验收。'}
