FROM centos:7

RUN echo "timeout=5" >> /etc/yum.conf && \
    yum update -y && \
    yum install -y epel-release

RUN yum install -y \
    gcc \
    make \
    openssl \
    openssl-devel \
    zlib-devel && \
        curl -O https://www.python.org/ftp/python/3.5.0/Python-3.5.0.tar.xz && \
        tar xf Python-3.5.0.tar.xz && \
        cd Python-3.5.0 && \
        ./configure && \
        make && \
        make install

# Install Coronado dependencies first so they can be cached
RUN pip3 install \
    argcomplete \
    argh \
    argparse \
    python-dateutil \
    tornado>=4.3

RUN pip3 install pylint>=1.5.0

# Install Coronado
COPY ./Coronado-2.0-py3.5.egg /root/Coronado-2.0-py3.5.egg
RUN easy_install-3.5 /root/Coronado-2.0-py3.5.egg

WORKDIR /root/WorkerPlugin
ENTRYPOINT ["./entrypoint.sh"]
COPY . /root/WorkerPlugin
