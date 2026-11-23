import importlib
import pkgutil


def import_module_tree(name: str, *, required: bool = False) -> None:
    """导入模块;它是包时连同全部子模块一起导入,让装饰器完成注册。

    模块本身不存在则跳过;模块内部的导入错误必须暴露,不能让 operation 静默缺失。
    """
    try:
        module = importlib.import_module(name)
    except ModuleNotFoundError as error:
        if required or error.name != name:
            raise
        return
    for info in pkgutil.walk_packages(getattr(module, "__path__", []), f"{name}."):
        importlib.import_module(info.name)
