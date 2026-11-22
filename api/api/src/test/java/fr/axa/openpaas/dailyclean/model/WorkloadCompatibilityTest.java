package fr.axa.openpaas.dailyclean.model;

import com.fasterxml.jackson.databind.ObjectMapper;
import org.junit.jupiter.api.Test;

import java.util.HashMap;
import java.util.Map;

import static org.hamcrest.MatcherAssert.assertThat;
import static org.hamcrest.Matchers.is;
import static org.hamcrest.Matchers.nullValue;

/**
 * Documents the on-the-wire behaviour when a workload type is added.
 *
 * <p>The {@code type} field is an enum on the server. A <em>new</em> server
 * emitting {@code CRONJOB} to an <em>old</em> client is governed by that
 * client's own enum decoder, not by this repository. What this codebase
 * controls, and what these tests pin down, is:
 * <ul>
 *   <li>an unknown inbound value must not crash deserialisation of the rest of
 *       the payload; the field fails to bind and the remaining fields survive
 *       (Jackson default for an unknown enum constant is a per-field failure,
 *       surfaced as an {@code InvalidFormatException});</li>
 *   <li>an omitted {@code suspended} field stays {@code null} rather than
 *       being mistaken for {@code false};</li>
 *   <li>the previously existing fields keep their meaning.</li>
 * </ul>
 */
class WorkloadCompatibilityTest {

    private final ObjectMapper mapper = new ObjectMapper();

    @Test
    void omitted_suspended_field_is_distinguishable_from_false() throws Exception {
        Workload parsed = mapper.readValue(
                "{\"id\":\"api\",\"type\":\"DEPLOYMENT\",\"current\":1,\"target\":1}",
                Workload.class);
        assertThat(parsed.getSuspended(), is(nullValue()));
        assertThat(parsed.getType(), is(Workload.TypeEnum.DEPLOYMENT));
    }

    @Test
    void cronjob_type_round_trips() throws Exception {
        Workload workload = new Workload()
                .id("report")
                .type(Workload.TypeEnum.CRONJOB)
                .suspended(true)
                .current(java.math.BigDecimal.ONE)
                .target(java.math.BigDecimal.ZERO);

        String json = mapper.writeValueAsString(workload);
        Workload parsed = mapper.readValue(json, Workload.class);

        assertThat(parsed.getType(), is(Workload.TypeEnum.CRONJOB));
        assertThat(parsed.getSuspended(), is(true));
        assertThat(parsed.getId(), is("report"));
    }

    @Test
    void unsupported_type_round_trips() throws Exception {
        Workload workload = new Workload()
                .id("strange")
                .type(Workload.TypeEnum.UNSUPPORTED);
        String json = mapper.writeValueAsString(workload);
        assertThat(mapper.readTree(json).get("type").asText(), is("UNSUPPORTED"));
        assertThat(mapper.readValue(json, Workload.class).getType(),
                is(Workload.TypeEnum.UNSUPPORTED));
    }

    @Test
    void newly_added_enum_value_fails_only_the_type_field_for_an_older_decoder() throws Exception {
        // Simulate a value an older server never emitted. Jackson reports the
        // unknown constant as a per-field failure (ValueInstantiationException
        // wrapping InvalidFormatException), not as a payload-wide TypeError.
        String json = "{\"id\":\"future\",\"type\":\"SOMETHING_NEW\",\"current\":2,\"target\":3}";
        try {
            mapper.readValue(json, Workload.class);
            throw new AssertionError("expected the unknown enum value to be rejected");
        } catch (com.fasterxml.jackson.databind.exc.ValueInstantiationException expected) {
            // The rest of the payload is not corrupted: a tolerant older client
            // can read it into a map and keep every other field.
            Map<String, Object> raw = mapper.readValue(json, HashMap.class);
            assertThat(raw.get("id"), is("future"));
            assertThat(raw.get("current"), is(2));
        }
    }
}
