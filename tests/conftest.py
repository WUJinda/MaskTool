"""pytest共享fixtures"""

import os

import pytest
from pathlib import Path


@pytest.fixture(autouse=True)
def _no_open_in_file_explorer(monkeypatch):
    """禁止任何用例真实弹出系统文件管理器窗口（R9，2026-09-21）。

    背景：test_save_on_click 把所有 st.button mock 成恒 True，
    保存面板的"📂 打开保存文件夹"被连带触发，os.startfile 真实
    弹出资源管理器（每跑一次测试弹一个，标题为 "out"）。在
    os.startfile 底层统一拦截：_open_in_explorer 及其各处
    from-import 副本全部经由它，patch 一处覆盖全部调用路径；
    不抛异常 → 调用方仍走"打开成功"分支，测试语义不变。
    """
    monkeypatch.setattr(os, "startfile", lambda path: None, raising=False)


@pytest.fixture
def sample_text():
    """示例文本，包含各类敏感信息"""
    return """
    合同编号：HT-XXXX-001

    甲方：某某建设集团有限公司
    乙方：某某科技有限公司

    项目名称：某某新区基础设施建设项目
    项目负责人：某人甲

    合同金额：1.2亿元
    联系电话：1XX-XXXX-XXXX
    身份证号：XXXXXXXXXXXXXXXXXX

    签约地点：某某市某某区
    签约日期：2024年3月15日
    """
