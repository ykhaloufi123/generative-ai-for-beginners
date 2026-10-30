# JMeter + plugin jmeter-iso8583 (tilln) et ses dépendances jPOS / BouncyCastle
FROM eclipse-temurin:17-jre
ARG JMETER_VERSION=5.6.3
ARG PLUGIN_VERSION=1.4
ENV JMETER_HOME=/opt/apache-jmeter-${JMETER_VERSION}
ENV PATH=${JMETER_HOME}/bin:${PATH}
RUN apt-get update && apt-get install -y --no-install-recommends curl && rm -rf /var/lib/apt/lists/* \
 && curl -fsSL https://archive.apache.org/dist/jmeter/binaries/apache-jmeter-${JMETER_VERSION}.tgz | tar -xz -C /opt \
 && curl -fsSL -o ${JMETER_HOME}/lib/ext/jmeter-iso8583-${PLUGIN_VERSION}.jar \
      https://github.com/tilln/jmeter-iso8583/releases/download/${PLUGIN_VERSION}/jmeter-iso8583-${PLUGIN_VERSION}.jar \
 && cd ${JMETER_HOME}/lib && for a in \
      org/jpos/jpos/2.1.10/jpos-2.1.10.jar \
      org/bouncycastle/bcprov-lts8on/2.73.6/bcprov-lts8on-2.73.6.jar \
      org/bouncycastle/bcpg-lts8on/2.73.6/bcpg-lts8on-2.73.6.jar \
      org/jdom/jdom2/2.0.6.1/jdom2-2.0.6.1.jar \
      org/osgi/org.osgi.core/6.0.0/org.osgi.core-6.0.0.jar \
      commons-cli/commons-cli/1.4/commons-cli-1.4.jar \
      org/yaml/snakeyaml/2.0/snakeyaml-2.0.jar \
      org/hdrhistogram/HdrHistogram/2.1.12/HdrHistogram-2.1.12.jar \
      org/javatuples/javatuples/1.2/javatuples-1.2.jar ; do \
      curl -fsSLO https://repo1.maven.org/maven2/$a ; done
WORKDIR /work
ENTRYPOINT ["jmeter"]
