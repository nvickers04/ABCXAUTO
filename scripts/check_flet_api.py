import flet as ft
print("app" in dir(ft), "run" in dir(ft))
print(getattr(ft, "app", None), getattr(ft, "run", None))