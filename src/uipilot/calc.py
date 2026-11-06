"""给 Agent 用的计算器：只接受四则运算和括号，用 Decimal 精确计算。

问题日志 P14：Sonnet 把 316,961.50 × 13% 算成 41,205.995（应为 41,204.995），
复现 Agent 被要求“独立再算一遍”时，三次都算出同样的错数。模型之间会共享同一种算术错误，
所以金额一律交给这个工具算。
"""

import ast
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation, getcontext

getcontext().prec = 40
_OPS = {ast.Add: lambda a, b: a + b, ast.Sub: lambda a, b: a - b,
        ast.Mult: lambda a, b: a * b, ast.Div: lambda a, b: a / b}


def _eval(node) -> Decimal:
    if isinstance(node, ast.Expression):
        return _eval(node.body)
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)) and not isinstance(node.value, bool):
        return Decimal(str(node.value))
    if isinstance(node, ast.BinOp) and type(node.op) in _OPS:
        return _OPS[type(node.op)](_eval(node.left), _eval(node.right))
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.USub, ast.UAdd)):
        v = _eval(node.operand)
        return -v if isinstance(node.op, ast.USub) else v
    raise ValueError("只支持数字、+ - * / 和括号")


def calculate(expression: str) -> str:
    expr = expression.replace(",", "").replace("×", "*").replace("÷", "/").replace("%", "/100")
    try:
        value = _eval(ast.parse(expr, mode="eval"))
    except (SyntaxError, ZeroDivisionError, InvalidOperation) as e:
        raise ValueError(f"无法计算：{e}") from e
    cents = value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return f"{expression} = {value.normalize():f}（四舍五入到分：{cents:,.2f}）"
