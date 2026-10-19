"""航班数据层。

    app.data.base            数据源公共接口与错误类型
    app.data.models          Flight / Route / AircraftInfo 数据模型
    app.data.registry        宽体机型表、特殊涂装表、航司兜底表、时区估算
    app.data.source_tar1090  tar1090 系镜像（adsb.fi 主源 / adsb.lol 备选）
    app.data.source_opensky  OpenSky 适配器（兜底源）
    app.data.source_fr24     Flightradar24 适配器（预留）
    app.data.sources         数据源工厂与降级链
    app.data.enrich          adsbdb / hexdb 富化与本地缓存
    app.data.manager         后台调度线程（拉取 → 富化 → 过滤 → 广播）
"""
