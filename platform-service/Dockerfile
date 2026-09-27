FROM maven:3.9.9-eclipse-temurin-21 AS build

WORKDIR /workspace
COPY pom.xml ./
RUN mvn -B -ntp dependency:go-offline

COPY src ./src
RUN mvn -B -ntp package -DskipTests

FROM eclipse-temurin:21-jre

WORKDIR /app
COPY --from=build /workspace/target/platform-service-0.1.0-SNAPSHOT.jar /app/platform-service.jar

USER 10001
EXPOSE 8081
ENTRYPOINT ["java", "-jar", "/app/platform-service.jar"]
