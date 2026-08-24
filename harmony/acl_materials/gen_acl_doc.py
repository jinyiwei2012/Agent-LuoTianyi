# -*- coding: utf-8 -*-
"""生成《ohos.permission.READ_IMAGEVIDEO 权限使用场景说明》Word 文档"""
import os
from docx import Document
from docx.shared import Pt, RGBColor, Cm
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml.ns import qn

OUT_DIR = r"C:\Agent-LuoTianyi\harmony\acl_materials"
os.makedirs(OUT_DIR, exist_ok=True)
OUT_PATH = os.path.join(OUT_DIR, "READ_IMAGEVIDEO_权限使用场景说明.docx")

doc = Document()

# 全局中文字体
style = doc.styles["Normal"]
style.font.name = "宋体"
style.font.size = Pt(12)
style._element.rPr.rFonts.set(qn("w:eastAsia"), "宋体")


def set_heading(text, level=1):
    h = doc.add_heading(text, level=level)
    for run in h.runs:
        run.font.name = "宋体"
        run._element.rPr.rFonts.set(qn("w:eastAsia"), "宋体")
        run.font.color.rgb = RGBColor(0, 0, 0)
    return h


def add_para(text, bold=False, align=None, indent=False):
    p = doc.add_paragraph()
    if indent:
        p.paragraph_format.first_line_indent = Cm(0.74)
    if align:
        p.alignment = align
    run = p.add_run(text)
    run.font.name = "宋体"
    run.font.size = Pt(12)
    run._element.rPr.rFonts.set(qn("w:eastAsia"), "宋体")
    run.bold = bold
    return p


# ===== 标题 =====
title = doc.add_heading("「洛天依」鸿蒙客户端权限使用场景说明", level=0)
for run in title.runs:
    run.font.name = "宋体"
    run._element.rPr.rFonts.set(qn("w:eastAsia"), "宋体")
add_para("申请权限：ohos.permission.READ_IMAGEVIDEO（读取用户图片/视频文件权限）", bold=True, align=WD_ALIGN_PARAGRAPH.CENTER)
add_para("申请主体：洛天依对话 Agent（AgentLuo）鸿蒙客户端", align=WD_ALIGN_PARAGRAPH.CENTER)
add_para("版本：v1.0.0（开发预览版）", align=WD_ALIGN_PARAGRAPH.CENTER)
add_para("日期：2026-08-24", align=WD_ALIGN_PARAGRAPH.CENTER)
doc.add_paragraph()

# ===== 一、应用简介 =====
set_heading("一、应用简介", 1)
add_para("「洛天依」是一款以虚拟歌手洛天依为角色的 AI 陪伴聊天应用。用户可以在应用中与洛天依进行文字聊天、语音互动，并通过发送照片的方式与洛天依分享日常、进行图片内容互动。洛天依（基于多模态大模型）能理解用户发送的图片内容并给出富有角色感的回复。", indent=True)
add_para("本应用为鸿蒙客户端，兼容 HarmonyOS NEXT（API 24）。", indent=True)

# ===== 二、权限使用场景 =====
set_heading("二、权限使用场景", 1)
set_heading("2.1 具体功能：向洛天依发送照片", 2)
add_para("在聊天界面中，用户点击输入栏左侧的「图片」按钮，系统调用系统相册选择器（Photo Access），用户从相册中选择一张照片。选中的照片将被读取并发送至服务端，由多模态大模型进行图片理解，洛天依据此生成一段与照片内容相关的回复（例如识别照片中的美食、风景、宠物等内容并展开话题）。", indent=True)

set_heading("2.2 权限用途", 2)
add_para("该权限仅用于：用户主动发起「发送照片」操作时，访问相册中的图片文件，完成照片选择与读取。这是应用实现「图片互动」核心功能所必需的能力，不用于任何其他目的。", indent=True)

set_heading("2.3 权限使用细节", 2)
add_para("1. 触发方式：所有图片访问均由用户在聊天界面点击「图片」按钮主动触发，应用不会在后台自动读取相册内容。", indent=True)
add_para("2. 读取范围：采用系统相册选择器（PhotoViewPicker），用户明确选择哪一张图片，应用仅读取该图片文件，不扫描、不遍历、不上传用户的全部相册内容。", indent=True)
add_para("3. 数据传输：所选图片仅在用户确认发送后，以 Base64 编码通过 HTTPS 加密通道上传至应用服务器，用于图片理解，不做其他用途。", indent=True)
add_para("4. 数据留存：图片用于单次对话回复生成，服务端不长期保存用户图片；客户端本地不上传、不外传相册元数据。", indent=True)

# ===== 三、权限必要性 =====
set_heading("三、权限必要性说明", 1)
add_para("「向洛天依发送照片」是应用宣称的核心功能之一，缺少该权限将导致用户无法从相册选择照片，图片互动功能完全不可用。该权限的申请范围（仅读取，不修改）与功能需求严格匹配，符合最小化原则。", indent=True)
add_para("应用不申请任何写入/删除相册权限（如 WRITE_IMAGEVIDEO、DELETE_IMAGEVIDEO），仅申请读取必要的图片文件。", indent=True)

# ===== 四、用户提示与撤销 =====
set_heading("四、用户提示与权限撤销", 1)
add_para("1. 应用首次触发「发送图片」时，会弹出系统权限申请弹窗，清楚告知用户权限用途（「用于选择图片发送给洛天依」）并询问同意，用户可选择拒绝。", indent=True)
add_para("2. 用户拒绝后，图片按钮仍可点击，但会提示「未选择图片」，不阻塞应用的文字聊天等其余功能。", indent=True)
add_para("3. 用户可在「设置 → 应用 → 权限管理」中随时撤销该权限；撤销后应用仅失去图片选择能力，其余功能不受影响。", indent=True)

# ===== 五、功能演示录屏 =====
set_heading("五、功能演示", 1)
add_para("配套提交《功能 demo 录屏》（mp4）：演示用户从聊天界面点击「图片」按钮 → 系统弹出权限申请弹窗 → 用户同意 → 打开相册选择器 → 选中一张照片 → 图片以气泡形式展示在聊天列表中 → 洛天依返回与该图片相关的回复，完整展示本权限的使用过程与场景。", indent=True)

# ===== 六、隐私合规承诺 =====
set_heading("六、隐私合规承诺", 1)
add_para("1. 应用严格遵守《个人信息保护法》与华为开发者协议，仅在用户明确授权且主动触发时访问图片。", indent=True)
add_para("2. 应用未开发任何后台相册扫描、批量读取、图片去重等越权行为。", indent=True)
add_para("3. 应用已在其隐私政策中明示图片权限用途，用户可查阅。", indent=True)

doc.save(OUT_PATH)
print("已生成:", OUT_PATH)
print("大小:", os.path.getsize(OUT_PATH), "bytes")
