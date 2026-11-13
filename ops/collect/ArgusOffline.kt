@file:JvmName("ArgusOffline")
package ru.sokomishalov.skraper.cli

import com.fasterxml.jackson.databind.ObjectMapper
import kotlinx.coroutines.flow.take
import kotlinx.coroutines.flow.toList
import kotlinx.coroutines.runBlocking
import ru.sokomishalov.skraper.client.*
import ru.sokomishalov.skraper.provider.facebook.FacebookSkraper
import ru.sokomishalov.skraper.provider.instagram.InstagramSkraper
import ru.sokomishalov.skraper.provider.odnoklassniki.OdnoklassnikiSkraper
import ru.sokomishalov.skraper.provider.vk.VkSkraper
import java.io.File

// The upstream parser receives one already guarded response. Pagination and
// media resolution cannot open sockets or create additional requests here.
fun main(args: Array<String>) = runBlocking {
    val mapper = ObjectMapper()
    val body = File(args[2]).readBytes()
    require(body.size <= 4 * 1024 * 1024)
    var supplied = false
    val client = object : SkraperClient {
        override suspend fun request(request: HttpRequest): HttpResponse {
            if (supplied) return HttpResponse(status = 200, body = ByteArray(0))
            supplied = true
            return HttpResponse(status = 200, body = body)
        }
    }
    val parser = when (args[0]) {
        "facebook" -> FacebookSkraper(client)
        "instagram" -> InstagramSkraper(client)
        "vk" -> VkSkraper(client)
        "ok" -> OdnoklassnikiSkraper(client)
        else -> error("unsupported_platform")
    }
    val limit = if (args.size > 4) args[4].toInt() else 5
    require(limit in 1..100)
    val rows = parser.getPosts(args[1]).take(limit).toList().map {
        mapOf("id" to it.id, "text" to it.text,
              "published" to it.publishedAt?.toString(),
              "media" to it.media.map { media -> media.url })
    }
    File(args[3]).writeText(mapper.writeValueAsString(rows))
}
