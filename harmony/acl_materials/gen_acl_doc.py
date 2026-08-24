# -*- coding: utf-8 -*-
"""
正式版《ohos.permission.READ_IMAGEVIDEO 权限使用场景说明》
封面页 / 目录 / 页眉页脚+页码 / 正式章节 / 表格化权限细节
"""
import os
from docx import Document
from docx.shared import Pt, Cm, RGBColor
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.enum.table import WD_TABLE_ALIGNMENT, WD_ALIGN_VERTICAL
from docx.oxml.ns import qn
from docx.oxml import OxmlElement

OUT_DIR = r"C:\Agent-LuoTianyi\harmony\acl_materials"
os.makedirs(OUT_DIR, exist_ok=True)
OUT_PATH = os.path.join(OUT_DIR, "READ_IMAGEVIDEO_权限使用场景说明.docx")
LOGO = r"C:\Agent-LuoTianyi\app\assets\images\icon.png"

ACCENT = RGBColor(0x16, 0x74, 0xA3)   # 主题蓝（accentText）
DARK = RGBColor(0x24, 0x34, 0x47)     # 正文深色
GRAY = RGBColor(0x7B, 0x87, 0x94)     # 次要灰

TITLE_FONT = "微软雅黑"
BODY_FONT = "宋体"


def set_font(run, name=BODY_FONT, size=12, bold=False, color=DARK):
    run.font.name = name
    run._element.rPr.rFonts.set(qn("w:eastAsia"), name)
    run._element.rPr.rFonts.set(qn("w:ascii"), name)
    run._element.rPr.rFonts.set(qn("w:hAnsi"), name)
    run.font.size = Pt(size)
    run.font.bold = bold
    run.font.color.rgb = color


def add_field(paragraph, instr_text):
    fld_begin = OxmlElement("w:fldChar"); fld_begin.set(qn("w:fldCharType"), "begin")
    instr = OxmlElement("w:instrText"); instr.set(qn("xml:space"), "preserve"); instr.text = instr_text
    fld_end = OxmlElement("w:fldChar"); fld_end.set(qn("w:fldCharType"), "end")
    r = paragraph.add_run()
    r._element.append(fld_begin)
    r._element.append(instr)
    r._element.append(fld_end)
    return r


def add_page_number(paragraph):
    run = paragraph.add_run("第 ")
    set_font(run, BODY_FONT, 10, color=GRAY)
    add_field(paragraph, "PAGE")
    run2 = paragraph.add_run(" 页　共 ")
    set_font(run2, BODY_FONT, 10, color=GRAY)
    add_field(paragraph, "NUMPAGES")
    run3 = paragraph.add_run(" 页")
    set_font(run3, BODY_FONT, 10, color=GRAY)


doc = Document()

# ---------- 页面设置 ----------
sec = doc.sections[0]
sec.page_width = Cm(21.0)
sec.page_height = Cm(29.7)
sec.top_margin = Cm(2.54)
sec.bottom_margin = Cm(2.54)
sec.left_margin = Cm(3.17)
sec.right_margin = Cm(3.17)

normal = doc.styles["Normal"]
normal.font.name = BODY_FONT
normal.font.size = Pt(12)
normal._element.rPr.rFonts.set(qn("w:eastAsia"), BODY_FONT)

# ---------- 页眉页脚 ----------
header = sec.header
hp = header.paragraphs[0]
hp.alignment = WD_ALIGN_PARAGRAPH.CENTER
hr = hp.add_run("「洛天依」鸿蒙客户端  ·  READ_IMAGEVIDEO 权限使用场景说明")
set_font(hr, TITLE_FONT, 9, color=GRAY)

footer = sec.footer
fp = footer.paragraphs[0]
fp.alignment = WD_ALIGN_PARAGRAPH.CENTER
add_page_number(fp)


def add_para(text, bold=False, align=None, size=12, color=DARK, indent=True,
             font=BODY_FONT, space_after=6, space_before=0):
    p = doc.add_paragraph()
    p.paragraph_format.line_spacing = 1.5
    p.paragraph_format.space_after = Pt(space_after)
    p.paragraph_format.space_before = Pt(space_before)
    if indent:
        p.paragraph_format.first_line_indent = Pt(24)
    if align:
        p.alignment = align
    run = p.add_run(text)
    set_font(run, font, size, bold, color)
    return p


def add_heading1(text):
    p = doc.add_paragraph()
    p.paragraph_format.space_before = Pt(18)
    p.paragraph_format.space_after = Pt(10)
    run = p.add_run(text)
    set_font(run, TITLE_FONT, 16, True, ACCENT)
    pPr = p._element.get_or_add_pPr()
    pbdr = OxmlElement("w:pBdr")
    bottom = OxmlElement("w:bottom")
    bottom.set(qn("w:val"), "single"); bottom.set(qn("w:sz"), "8")
    bottom.set(qn("w:space"), "1"); bottom.set(qn("w:color"), "1674A3")
    pbdr.append(bottom)
    pPr.append(pbdr)
    return p


def add_heading2(text):
    p = doc.add_paragraph()
    p.paragraph_format.space_before = Pt(12)
    p.paragraph_format.space_after = Pt(6)
    run = p.add_run(text)
    set_font(run, TITLE_FONT, 14, True, DARK)
    return p


def add_table(rows, header_row=True):
    table = doc.add_table(rows=len(rows), cols=len(rows[0]))
    table.style = "Table Grid"
    table.alignment = WD_TABLE_ALIGNMENT.CENTER
    for i, row in enumerate(rows):
        for j, cell_text in enumerate(row):
            cell = table.cell(i, j)
            cell.vertical_alignment = WD_ALIGN_VERTICAL.CENTER
            p = cell.paragraphs[0]
            run = p.add_run(cell_text)
            if header_row and i == 0:
                set_font(run, TITLE_FONT, 11, True, RGBColor(0xFF, 0xFF, 0xFF))
                shd = OxmlElement("w:shd")
                shd.set(qn("w:val"), "clear"); shd.set(qn("w:fill"), "1674A3")
                cell._element.get_or_add_tcPr().append(shd)
            else:
                set_font(run, BODY_FONT, 11, False, DARK)
            p.paragraph_format.line_spacing = 1.3
        # 禁止行拆分
        trPr = table.rows[i]._tr.get_or_add_trPr()
        cantSplit = OxmlElement("w:cantSplit")
        trPr.append(cantSplit)
        # 表头行跨页重复（tblHeader：换页后自动重复表头，专业规范）
        if header_row and i == 0:
            tblHeader = OxmlElement("w:tblHeader")
            trPr.append(tblHeader)
    doc.add_paragraph().paragraph_format.space_after = Pt(4)
    return table


# =========================================================
# 封面页
# =========================================================
for _ in range(4):
    doc.add_paragraph()

p = doc.add_paragraph(); p.alignment = WD_ALIGN_PARAGRAPH.CENTER
run = p.add_run("「洛天依」鸿蒙客户端")
set_font(run, TITLE_FONT, 20, True, DARK)

p = doc.add_paragraph(); p.alignment = WD_ALIGN_PARAGRAPH.CENTER
run = p.add_run("权限使用场景说明")
set_font(run, TITLE_FONT, 26, True, ACCENT)

doc.add_paragraph()

try:
    p = doc.add_paragraph(); p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    run = p.add_run()
    run.add_picture(LOGO, width=Cm(3.2))
except Exception as e:
    print("logo err:", e)

doc.add_paragraph()
p = doc.add_paragraph(); p.alignment = WD_ALIGN_PARAGRAPH.CENTER
run = p.add_run("申请权限：ohos.permission.READ_IMAGEVIDEO")
set_font(run, TITLE_FONT, 14, True, DARK)

p = doc.add_paragraph(); p.alignment = WD_ALIGN_PARAGRAPH.CENTER
run = p.add_run("（读取用户图片/视频文件权限）")
set_font(run, BODY_FONT, 12, False, GRAY)

for _ in range(3):
    doc.add_paragraph()

info_rows = [
    ["申请主体", "洛天依对话 Agent（AgentLuo）"],
    ["应用名称", "洛天依"],
    ["客户端平台", "HarmonyOS NEXT（API 24）"],
    ["应用版本", "v1.0.0（开发预览版）"],
    ["材料类型", "权限申请阶段——使用场景说明"],
    ["编制日期", "2026 年 08 月 24 日"],
    ["文档密级", "对外提交（应用市场审核用）"],
]
t = doc.add_table(rows=len(info_rows), cols=2)
t.alignment = WD_TABLE_ALIGNMENT.CENTER
t.style = "Table Grid"
for i, (k, v) in enumerate(info_rows):
    c0, c1 = t.cell(i, 0), t.cell(i, 1)
    c0.width = Cm(5.0)
    p0 = c0.paragraphs[0]; r0 = p0.add_run(k); set_font(r0, TITLE_FONT, 11, True, DARK)
    shd = OxmlElement("w:shd"); shd.set(qn("w:val"), "clear"); shd.set(qn("w:fill"), "EEF4F8")
    c0._element.get_or_add_tcPr().append(shd)
    c1.width = Cm(9.5)
    p1 = c1.paragraphs[0]; r1 = p1.add_run(v); set_font(r1, BODY_FONT, 11, False, DARK)

doc.add_page_break()

# =========================================================
# 目录页
# =========================================================
p = doc.add_paragraph(); p.alignment = WD_ALIGN_PARAGRAPH.CENTER
run = p.add_run("目  录")
set_font(run, TITLE_FONT, 18, True, DARK)
doc.add_paragraph()

p = doc.add_paragraph()
run = p.add_run()
add_field(p, r'TOC \o "1-3" \h \z \u')
for r in p.runs:
    set_font(r, BODY_FONT, 11, False, GRAY)

doc.add_page_break()

# =========================================================
# 正文
# =========================================================
add_heading1("一、应用简介")
add_para("「洛天依」是一款以虚拟歌手洛天依为角色的 AI 陪伴聊天应用。用户可以在应用中与洛天依进行文字聊天、语音互动，并通过发送照片的方式与洛天依分享日常、进行图片内容互动。洛天依（基于多模态大模型）能够理解用户发送的图片内容，并给出富有角色感的回复。")
add_para("本应用为鸿蒙客户端，兼容 HarmonyOS NEXT（API 24），实现聊天、语音、图片互动等核心功能。")

add_heading1("二、权限使用场景")
add_heading2("2.1 具体功能：向洛天依发送照片")
add_para("在聊天界面中，用户点击输入栏左侧的「图片」按钮，应用调用系统相册选择器（Photo Access），用户从相册中选择一张照片。选中的照片将被读取并发送至服务端，由多模态大模型进行图片理解，洛天依据此生成一段与照片内容相关的回复（例如识别照片中的美食、风景、宠物等内容并展开话题）。")

add_heading2("2.2 权限用途")
add_para("该权限仅用于：用户主动发起「发送照片」操作时，访问相册中的图片文件，完成照片选择与读取。这是应用实现「图片互动」核心功能所必需的能力，不用于任何其他目的。")

add_heading2("2.3 权限使用细节")
add_table([
    ["序号", "使用环节", "具体说明"],
    ["1", "触发方式", "所有图片访问均由用户在聊天界面点击「图片」按钮主动触发，应用不会在后台自动读取相册内容"],
    ["2", "读取范围", "采用系统相册选择器（PhotoViewPicker），用户明确选择哪一张图片，应用仅读取该图片文件，不扫描、不遍历、不上传用户的全部相册内容"],
    ["3", "数据传输", "所选图片仅在用户确认发送后，以 Base64 编码通过 HTTPS 加密通道上传至应用服务器，用于图片理解，不做其他用途"],
    ["4", "数据留存", "图片用于单次对话回复生成，服务端不长期保存用户图片；客户端本地不上传、不外传相册元数据"],
])

add_heading1("三、权限必要性说明")
add_para("「向洛天依发送照片」是应用宣称的核心功能之一，缺少该权限将导致用户无法从相册选择照片，图片互动功能完全不可用。该权限的申请范围（仅读取，不修改）与功能需求严格匹配，符合最小化原则。")
add_para("应用不申请任何写入/删除相册权限（如 WRITE_IMAGEVIDEO、DELETE_IMAGEVIDEO），仅申请读取必要的图片文件。")

add_heading1("四、用户提示与权限撤销")
add_table([
    ["项目", "说明"],
    ["首次提示", "应用首次触发「发送图片」时，会弹出系统权限申请弹窗，清楚告知用户权限用途（「用于选择图片发送给洛天依」）并询问同意，用户可选择拒绝"],
    ["拒绝处理", "用户拒绝后，图片按钮仍可点击，但会提示「未选择图片」，不阻塞应用的文字聊天等其余功能"],
    ["撤销方式", "用户可在「设置 → 应用 → 权限管理」中随时撤销该权限；撤销后应用仅失去图片选择能力，其余功能不受影响"],
])

add_heading1("五、功能演示")
add_para("配套提交《功能 demo 录屏》（README_IMAGEVIDEO_功能demo录屏.mp4），演示流程：")
add_table([
    ["步骤", "演示内容"],
    ["①", "首页，点击「进入聊天」"],
    ["②", "进入聊天页（ChatPage），输入栏含「图片」按钮"],
    ["③", "点击「图片」按钮，系统拉起相册选择器"],
    ["④", "相册选择器展示：系统「安全访问图库」受限模式，应用仅可访问用户选定的图片和视频——即 READ_IMAGEVIDEO 权限使用场景"],
])

add_heading1("六、隐私合规承诺")
add_para("1. 应用严格遵守《中华人民共和国个人信息保护法》及华为开发者协议，仅在用户明确授权且主动触发时访问图片。")
add_para("2. 应用未开发任何后台相册扫描、批量读取等越权行为。")
add_para("3. 应用已在其隐私政策中明示图片权限用途，用户可随时查阅。")
add_para("4. 权限申请遵循最小化与必要性原则，仅申请实现核心功能所需的读取权限。")

doc.add_paragraph()
p = doc.add_paragraph(); p.alignment = WD_ALIGN_PARAGRAPH.RIGHT
run = p.add_run("申请方（盖章）：__________________")
set_font(run, BODY_FONT, 12, False, DARK)
p = doc.add_paragraph(); p.alignment = WD_ALIGN_PARAGRAPH.RIGHT
run = p.add_run("日期：____ 年 ____ 月 ____ 日")
set_font(run, BODY_FONT, 12, False, DARK)

doc.save(OUT_PATH)
print("已生成:", OUT_PATH)
print("大小:", os.path.getsize(OUT_PATH), "bytes")
