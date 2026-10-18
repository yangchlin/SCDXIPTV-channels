#基于的基础镜像
FROM python:3.11.6-slim-bullseye

# 设置app文件夹是工作目录
WORKDIR /app

# 先复制依赖文件，利用 Docker 缓存层
COPY requirements.txt .

# 安装系统依赖和 Python 依赖
RUN apt-get update && \
    apt-get install -y --no-install-recommends cron nano && \
    ln -snf /usr/share/zoneinfo/Asia/Shanghai /etc/localtime && \
    echo "Asia/Shanghai" > /etc/timezone && \
    rm -rf /var/lib/apt/lists/* && \
    apt-get clean 

# 安装支持
ENV TZ=Asia/Shanghai
ENV LANG=C.UTF-8
ENV LC_ALL C.UTF-8
ENV PYTHONUNBUFFERED=0
ENV FLASK_APP=app.py

RUN pip install --no-cache-dir -r requirements.txt

# DES3 补丁
RUN sed -i '84 s/^/#/' /usr/local/lib/python3.11/site-packages/Crypto/Cipher/DES3.py && \
    sed -i '85 s/^/#/' /usr/local/lib/python3.11/site-packages/Crypto/Cipher/DES3.py

RUN ln -snf /usr/share/zoneinfo/$TIME_ZONE /etc/localtime && echo $TIME_ZONE > /etc/timezone

COPY . /app

# start.sh 权限放在构建前或构建时设置
# 添加启动脚本并设置执行权限
RUN chmod +x /app/start.sh

EXPOSE 3000

# 使用启动脚本作为容器的入口点
CMD ["/bin/bash", "/app/start.sh"]
