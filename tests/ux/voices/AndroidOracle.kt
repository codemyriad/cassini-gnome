import java.io.File
import org.json.JSONArray
import org.json.JSONObject
import org.cassini.core.format.Word
import org.cassini.core.speakers.*

fun objectMap(o: JSONObject): Map<String, Any> = o.keys().asSequence().associateWith { o.get(it) }
fun array(a: JSONArray): List<Any> = (0 until a.length()).map { a.get(it) }
fun vector(v: Any): FloatArray {
    if (v is JSONArray) return FloatArray(v.length()) {
        when(val n = v.get(it)) { is Number -> n.toFloat(); else -> n.toString().toFloat() }
    }
    val o = v as JSONObject
    if (o.has("normalise")) return Voiceprints.normalised(vector(o.get("normalise")))!!
    val a = FloatArray(o.getInt("dim"))
    if (o.has("axis")) a[o.getInt("axis")] = 1f
    else {
        val s = o.getDouble("near").toFloat()
        a[0] = s; a[o.getInt("other")] = kotlin.math.sqrt(1 - s * s)
    }
    return a
}
fun voices(o: JSONObject) = array(o.getJSONArray("voices")).map { it as JSONObject }.map {
    Voice(it.getString("id"), it.getString("name"), it.getString("model"), vector(it.get("mean")), 1, 10.0, 0, 0)
}
fun turns(o: JSONObject) = array(o.getJSONArray("turns")).map { it as JSONObject }.map {
    SpeakerTurn(it.getLong("startMs"), it.getLong("endMs"), it.getInt("speaker"))
}
fun words(o: JSONObject) = array(o.getJSONArray("words")).map { it as JSONObject }.map {
    Word(it.optString("speaker",it.optString("speakerId")), it.getLong("startMs"), it.getLong("endMs"), it.getString("text"))
}
fun windows(o: JSONObject): JSONObject {
    val selected = Voiceprints.select(turns(o), words(o), o.getInt("sampleRate"), o.getInt("totalSamples"))
    return JSONObject().apply { selected.forEach { (s, spans) -> put(s,JSONArray(spans.map { listOf(it.startSample,it.endSample) })) } }
}
fun run(op: String, o: JSONObject): Any? = when(op) {
    "normalise" -> Voiceprints.normalised(vector(o.get("vector")))?.toList()
    "cosine" -> Voiceprints.cosine(vector(o.get("a")), vector(o.get("b")))
    "select" -> windows(o)
    "dimension" -> org.cassini.core.models.ModelCatalog.VOICEPRINT_DIM
    "stale" -> VoiceStaleness.isStale(Voice("v","Anna",o.getString("model"),FloatArray(o.getInt("dimension")),1,5.0,0,0))
    "normaliseName" -> NameMatch.normalise(o.getString("name"))
    "nameCandidates" -> NameMatch.samePersonCandidates(o.getString("typed"),voices(o)).map { it.id }
    "namePick" -> NameMatch.samePerson(o.getString("typed"), voices(o),objectMap(o.getJSONObject("scores")).mapValues { (it.value as Number).toFloat() })?.id
    "rank" -> NameMatch.rank(if(o.isNull("print")) null else vector(o.get("print")),voices(o),o.getDouble("suggest").toFloat()).map {
        JSONObject().put("id",it.voice.id).put("score",it.score ?: JSONObject.NULL).put("stale",it.stale).put("likely",it.likely)
    }
    "match" -> {
        val t = o.getJSONObject("thresholds")
        val excluded = objectMap(o.getJSONObject("excluded")).mapValues { array(it.value as JSONArray).map { it.toString() }.toSet() }
        val matches = VoiceMatcher.match(objectMap(o.getJSONObject("speakers")).mapValues { vector(it.value) }, voices(o), o.getString("model"),
             Thresholds(t.getDouble("auto").toFloat(),t.getDouble("suggest").toFloat(),t.getDouble("margin").toFloat()), excluded)
        JSONObject().apply { matches.forEach { (s,m) -> put(s,JSONObject().put("voiceId",m.voiceId).put("state",m.state.name.lowercase())) } }
    }
    "decodeVersion" -> try { VoiceCodec.decode(o.toString()); error("unsupported version accepted") } catch(e: VoiceCodec.UnsupportedVersion) {
        JSONObject().put("unsupportedVersion", e.found)
    }
    "voiceRoundtrip" -> {
        val (vs,_) = VoiceCodec.enrol(emptyList(),null,o.getString("name"),o.getString("model"),vector(o.get("embedding")),o.getDouble("seconds"),o.getLong("now"))
        val v = VoiceCodec.decode(VoiceCodec.encode(vs)).single()
        check(v.id.startsWith("voice_"))
        v.json().apply { remove("id"); put("idPrefix","voice_"); put("version",1) }
    }
    "enrolUnenrol" -> {
        val (first,a) = VoiceCodec.enrol(emptyList(),null,"Anna","m",vector(o.get("first")),1.0,0)
        val (_,twice) = VoiceCodec.enrol(first,a.id,"Anna","m",vector(o.get("second")),1.0,0)
        VoiceCodec.unenrol(twice,vector(o.get("undo")))?.let { JSONObject().put("mean",it.mean.toList()).put("count",it.count).put("seconds",it.seconds) }
    }
    else -> error("Unknown operation $op")
}
fun main(args: Array<String>) {
    val doc = JSONObject(File(args[0]).readText())
    val output = JSONObject()
    for(c in array(doc.getJSONArray("cases"))) {
        val test = c as JSONObject
        val actual = JSONArray()
        for (item in array(test.getJSONArray("checks"))) {
            val v = item as JSONObject
            val result = run(v.getString("operation"),v.getJSONObject("input"))
            actual.put(if(result==null) JSONObject.NULL else JSONObject.wrap(result))
        }
        output.put(test.getString("id"),actual)
    }
    File(args[1]).writeText(output.toString(2)+"\n")
    if(args.size>=4) {
        val golden = JSONObject(File(args[2]).readText())
        val e=golden.getJSONObject("expected")
        val w=words(e)
        val t=turns(e)
        val n=golden.getJSONObject("input").getInt("frames")
        val sets=linkedMapOf("full" to w, "first" to w.filter { it.startMs<19000 }, "second" to w.filter { it.startMs>=19000 })
        val selected=JSONObject()
        for((name,subset) in sets) {
            val spans=Voiceprints.select(t,subset,16000,n)
            selected.put(name,JSONObject().apply { spans.forEach { (s,x) -> put(s,JSONArray(x.map { listOf(it.startSample,it.endSample) })) } })
        }
        File(args[3]).writeText(selected.toString(2)+"\n")
    }
}
