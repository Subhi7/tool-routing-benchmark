"""Canonical model input.

Every model sees the same information: the conversation, then a lettered list
of choices. Tools are shuffled with a per-example seed; NO_TOOL is always the
last choice (a natural "none of the above") and is offered on EVERY example,
including `multiple` ones, so its presence never leaks the category.

Adapters that need per-choice text (e.g. GLiClass labels) use `choice_text`,
which is exactly the block shown under that letter in the prompt.
"""
import json
import random
import string

NO_TOOL = "NO_TOOL"
# The one instruction every model receives, verbatim.
INSTRUCTION = ("Which of the available tools should be called to handle the final user request? "
               "Choose NO_TOOL if none of the available tools should be used.")
NO_TOOL_TEXT = "NO_TOOL\nNone of the available tools should be used."
LETTERS = string.ascii_uppercase + "".join(f"{a}{b}" for a in "AB" for b in string.ascii_uppercase)


def tool_text(tool):
    params = json.dumps(tool.get("parameters", {}), ensure_ascii=False, separators=(",", ": "))
    return f"Name: {tool['name']}\nDescription: {tool.get('description', '')}\nParameters: {params}"


def render_context(messages):
    if len(messages) == 1 and messages[0]["role"] == "user":
        return f"User request:\n{messages[0]['content']}"
    lines = [f"[{m['role']}]\n{m['content']}" for m in messages]
    return "Conversation (respond to the final user message):\n" + "\n\n".join(lines)


def build_choices(example, order="seeded", seed=0):
    """order: 'seeded' (per-example shuffle), 'reversed' (reverse of seeded),
    or 'original' (BFCL order). NO_TOOL is always appended last."""
    tools = list(example["tools"])
    if order in ("seeded", "reversed"):
        random.Random(f"{seed}:{example['id']}").shuffle(tools)
        if order == "reversed":
            tools.reverse()
    elif order != "original":
        raise ValueError(order)
    choices = [dict(id=LETTERS[i], tool=t["name"], choice_text=tool_text(t)) for i, t in enumerate(tools)]
    choices.append(dict(id=LETTERS[len(tools)], tool=NO_TOOL, choice_text=NO_TOOL_TEXT))
    return choices


def render(example, order="seeded", seed=0):
    """Returns the same information in two shapes: `prompt` (one string, for
    inspection and single-text models) and context/instruction/choices (for
    APIs that take content and choices separately)."""
    choices = build_choices(example, order, seed)
    context = render_context(example["messages"])
    body = "\n\n".join(f"{c['id']}\n{c['choice_text']}" for c in choices)
    prompt = f"{context}\n\nAvailable tools:\n\n{body}"
    gold_id = next(c["id"] for c in choices if c["tool"] == example["gold"])
    return dict(prompt=prompt, context=context, instruction=INSTRUCTION,
                choices=choices, gold_id=gold_id)
