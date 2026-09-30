FROM maven:3.9.9-eclipse-temurin-21 AS build

WORKDIR /workspace
COPY pom.xml ./
RUN mvn -B -ntp dependency:go-offline

COPY src ./src
RUN mvn -B -ntp package -DskipTests

FROM eclipse-temurin:21-jre

RUN apt-get update \
    && apt-get upgrade -y \
    && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY --from=build /workspace/target/platform-service-0.1.0-SNAPSHOT.jar /app/platform-service.jar

USER 10001
EXPOSE 8081
HEALTHCHECK --interval=30s --timeout=5s --start-period=45s --retries=4 \
    CMD curl --fail --silent --show-error http://127.0.0.1:8081/actuator/health >/dev/null || exit 1
ENTRYPOINT ["java", "-jar", "/app/platform-service.jar"]
