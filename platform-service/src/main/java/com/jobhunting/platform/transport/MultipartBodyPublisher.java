package com.jobhunting.platform.transport;

import java.io.ByteArrayInputStream;
import java.io.IOException;
import java.io.InputStream;
import java.io.UncheckedIOException;
import java.net.http.HttpRequest;
import java.nio.ByteBuffer;
import java.nio.charset.StandardCharsets;
import java.util.ArrayList;
import java.util.Collections;
import java.util.List;
import java.util.concurrent.Flow;
import java.util.concurrent.atomic.AtomicBoolean;
import org.springframework.web.multipart.MultipartFile;

/**
 * Streams a bounded multipart request to an internal service without buffering
 * the uploaded file in a second byte array.
 */
public final class MultipartBodyPublisher {
    private MultipartBodyPublisher() {}

    public record Field(String name, String value, String contentType) {}
    public record FilePart(String name, String filename, String contentType, MultipartFile file) {}

    public static HttpRequest.BodyPublisher publisher(String boundary, List<Field> fields, List<FilePart> files) {
        List<Part> parts = new ArrayList<>();
        long length = 0;
        for (Field field : fields) {
            byte[] prefix = fieldHeader(boundary, field.name(), field.contentType());
            byte[] value = field.value().getBytes(StandardCharsets.UTF_8);
            byte[] suffix = "\r\n".getBytes(StandardCharsets.UTF_8);
            parts.add(new Part(prefix, value, suffix));
            length = add(length, prefix.length + value.length + suffix.length);
        }
        for (FilePart file : files) {
            byte[] prefix = fileHeader(boundary, file);
            byte[] suffix = "\r\n".getBytes(StandardCharsets.UTF_8);
            parts.add(new Part(prefix, file.file(), suffix));
            length = add(length, prefix.length + file.file().getSize() + suffix.length);
        }
        byte[] end = ("--" + boundary + "--\r\n").getBytes(StandardCharsets.UTF_8);
        length = add(length, end.length);
        final long contentLength = length;
        return new HttpRequest.BodyPublisher() {
            @Override public long contentLength() { return contentLength; }

            @Override public void subscribe(Flow.Subscriber<? super ByteBuffer> subscriber) {
                List<InputStream> streams = new ArrayList<>();
                try {
                    for (Part part : parts) {
                        streams.add(new ByteArrayInputStream(part.prefix()));
                        if (part.value() != null) streams.add(new ByteArrayInputStream(part.value()));
                        if (part.file() != null) streams.add(part.file().getInputStream());
                        streams.add(new ByteArrayInputStream(part.suffix()));
                    }
                    streams.add(new ByteArrayInputStream(end));
                    HttpRequest.BodyPublishers.ofInputStream(
                            () -> new java.io.SequenceInputStream(Collections.enumeration(streams)))
                        .subscribe(subscriber);
                } catch (IOException error) {
                    subscriber.onSubscribe(new Flow.Subscription() {
                        @Override public void request(long ignored) {}
                        @Override public void cancel() {}
                    });
                    subscriber.onError(new UncheckedIOException(error));
                }
            }
        };
    }

    /** Prevents a state-changing internal callback from being replayed. */
    public static HttpRequest.BodyPublisher nonReplayable(HttpRequest.BodyPublisher delegate) {
        AtomicBoolean used = new AtomicBoolean();
        return new HttpRequest.BodyPublisher() {
            @Override public long contentLength() { return delegate.contentLength(); }

            @Override public void subscribe(Flow.Subscriber<? super ByteBuffer> subscriber) {
                if (used.compareAndSet(false, true)) {
                    delegate.subscribe(subscriber);
                    return;
                }
                subscriber.onSubscribe(new Flow.Subscription() {
                    @Override public void request(long ignored) {}
                    @Override public void cancel() {}
                });
                subscriber.onError(new IOException("Multipart replay disabled"));
            }
        };
    }

    private record Part(byte[] prefix, byte[] value, MultipartFile file, byte[] suffix) {
        Part(byte[] prefix, byte[] value, byte[] suffix) { this(prefix, value, null, suffix); }
        Part(byte[] prefix, MultipartFile file, byte[] suffix) { this(prefix, null, file, suffix); }
    }

    private static byte[] fieldHeader(String boundary, String name, String contentType) {
        String header = "--" + boundary + "\r\nContent-Disposition: form-data; name=\""
                + headerValue(name) + "\"\r\n" + contentHeader(contentType) + "\r\n\r\n";
        return header.getBytes(StandardCharsets.UTF_8);
    }

    private static byte[] fileHeader(String boundary, FilePart file) {
        String header = "--" + boundary + "\r\nContent-Disposition: form-data; name=\""
                + headerValue(file.name()) + "\"; filename=\"" + headerValue(file.filename()) + "\"\r\n"
                + contentHeader(file.contentType()) + "\r\n\r\n";
        return header.getBytes(StandardCharsets.UTF_8);
    }

    private static String contentHeader(String contentType) {
        return "Content-Type: " + (contentType == null || contentType.isBlank()
                ? "application/octet-stream" : headerValue(contentType));
    }

    private static String headerValue(String value) {
        return (value == null ? "" : value).replace('\r', '_').replace('\n', '_').replace('"', '_');
    }

    private static long add(long left, long right) {
        return Math.addExact(left, right);
    }
}
