import os
from pdf2image import convert_from_path
from PIL import Image

base_path = r"C:\Users\zgj\Documents\Claude\Projects\summer\ai-supply"
poppler_path = r"E:\base\poppler-26.02.0\Library\bin"

pdf_path = os.path.join(base_path, r"趋势报告\山系户外童装花型TOP热榜.pdf")
output_dir = os.path.join(base_path, r"趋势报告\山系户外童装花型TOP热榜")  # 你想保存的子目录
max_size = 2048  # 输入最长边尺寸，例如1024像素

os.makedirs(output_dir, exist_ok=True)

pages = convert_from_path(pdf_path, dpi=300, poppler_path=poppler_path)  # dpi越高，解析图表越清晰

image_files = []
for i, page in enumerate(pages):
    # 获取原始尺寸
    width, height = page.size

    # 计算缩放比例
    scale = max_size / max(width, height)
    if scale < 1:  # 仅当图片大于指定尺寸才缩放
        new_width = int(width * scale)
        new_height = int(height * scale)
        page = page.resize((new_width, new_height), Image.LANCZOS) # type: ignore

    file_name = f"page_{i}.png"
    file_path = os.path.join(output_dir, file_name)  # 拼接到子目录
    page.save(file_path, "PNG")
    image_files.append(file_name)

print("PDF 转图片完成：", image_files)